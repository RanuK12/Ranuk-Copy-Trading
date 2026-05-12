"""Market scanner — the bot's "eyes".

Every :data:`CFG.scan_interval` seconds this module:

1. Hits Gamma for all active/open markets.
2. Classifies each market into buckets that strategies care about
   (arbitrage candidate, tail-end candidate, micro-spread candidate,
   crypto 15-min, etc.).
3. Enriches the top N candidates with best-bid/best-ask from the CLOB
   orderbook (bounded to stay inside the 60/min rate limit).
4. Publishes a :class:`MarketSnapshot` that every strategy reads from.

Strategies never hit Gamma themselves — they pull from the snapshot and
emit :class:`Opportunity` objects into the shared queue. This is what
lets us run 7 strategies on a single rate-limited API.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from bot.clients.polymarket import PolyMarket, get_poly
from bot.config import CFG
from bot.logger import get_logger

log = get_logger("scanner")


# Match common 15-minute crypto-price market slugs
_CRYPTO_15M_RE = re.compile(
    r"(bitcoin|btc|ethereum|eth|solana|sol).*(15\s*min|5\s*min|hour|up|down|above|below)",
    re.IGNORECASE,
)


@dataclass
class EnrichedMarket:
    """A PolyMarket with live orderbook prices (best bid / best ask)."""

    market: PolyMarket
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    enriched_at: float = 0.0

    # ---- Helpers ---------------------------------------------------------
    @property
    def yes_mid(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    @property
    def no_mid(self) -> Optional[float]:
        if self.no_bid is None or self.no_ask is None:
            return None
        return (self.no_bid + self.no_ask) / 2

    @property
    def yes_spread(self) -> Optional[float]:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    @property
    def sum_yes_no(self) -> Optional[float]:
        """Sum of YES ask + NO ask (used for sum-to-one arbitrage)."""
        if self.yes_ask is None or self.no_ask is None:
            return None
        return self.yes_ask + self.no_ask

    def days_to_resolution(self) -> Optional[float]:
        ed = self.market.end_date
        if not ed:
            return None
        try:
            dt = datetime.fromisoformat(ed.replace("Z", "+00:00"))
            delta = dt - datetime.now(timezone.utc)
            return delta.total_seconds() / 86400
        except Exception:  # noqa: BLE001
            return None

    def is_crypto_15m(self) -> bool:
        text = f"{self.market.slug} {self.market.question}".lower()
        return bool(_CRYPTO_15M_RE.search(text))


@dataclass
class MarketSnapshot:
    """Shared read-only view of the market universe, refreshed every 5s."""

    markets: dict[str, EnrichedMarket] = field(default_factory=dict)
    generated_at: float = 0.0
    scan_duration_seconds: float = 0.0

    # Pre-computed buckets for cheap strategy access
    arbitrage_candidates: list[EnrichedMarket] = field(default_factory=list)
    tail_end_candidates: list[EnrichedMarket] = field(default_factory=list)
    micro_spread_candidates: list[EnrichedMarket] = field(default_factory=list)
    crypto_15m_markets: list[EnrichedMarket] = field(default_factory=list)
    sniper_candidates: list[EnrichedMarket] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------
class MarketScanner:
    """Builds a fresh :class:`MarketSnapshot` on a fixed cadence."""

    def __init__(self, *, max_enrich_per_scan: int = 40) -> None:
        self._poly = get_poly()
        self._max_enrich = max_enrich_per_scan
        self._snapshot = MarketSnapshot()
        self._lock = asyncio.Lock()
        self._errors = 0

    @property
    def snapshot(self) -> MarketSnapshot:
        return self._snapshot

    # ------------------------------------------------------------------
    # Main scan cycle
    # ------------------------------------------------------------------
    async def scan_once(self) -> MarketSnapshot:
        t0 = time.monotonic()
        try:
            raw = await self._poly.fetch_active_markets_async(limit=500)
        except Exception as e:  # noqa: BLE001
            self._errors += 1
            log.warning(f"[yellow]Gamma fetch failed:[/] {e}")
            return self._snapshot

        enriched: dict[str, EnrichedMarket] = {}
        for m in raw:
            enriched[m.condition_id] = EnrichedMarket(market=m)

        # Pick the most promising candidates for orderbook enrichment.
        # Heuristics (cheap, pre-orderbook):
        #   * highest volume first  -> better liquidity
        #   * prefer crypto 15m     -> DipArb / MM / Micro-Spread
        candidates = sorted(
            enriched.values(),
            key=lambda e: (
                0 if e.is_crypto_15m() else 1,
                -e.market.volume_usdc,
            ),
        )[: self._max_enrich]

        await asyncio.gather(
            *(self._enrich(em) for em in candidates), return_exceptions=True
        )

        snap = MarketSnapshot(
            markets=enriched,
            generated_at=time.time(),
            scan_duration_seconds=time.monotonic() - t0,
        )
        self._bucket(snap)
        async with self._lock:
            self._snapshot = snap
        self._errors = 0
        log.debug(
            f"scan: {len(enriched)} markets | enriched={len(candidates)} | "
            f"arb={len(snap.arbitrage_candidates)} "
            f"tail={len(snap.tail_end_candidates)} "
            f"micro={len(snap.micro_spread_candidates)} "
            f"crypto={len(snap.crypto_15m_markets)} "
            f"sniper={len(snap.sniper_candidates)} "
            f"in {snap.scan_duration_seconds:.2f}s"
        )
        return snap

    async def _enrich(self, em: EnrichedMarket) -> None:
        try:
            book_yes = await self._poly.get_order_book_async(em.market.yes_token_id)
            book_no = await self._poly.get_order_book_async(em.market.no_token_id)
            em.yes_bid, em.yes_ask = _best_bid_ask(book_yes)
            em.no_bid, em.no_ask = _best_bid_ask(book_no)
            em.enriched_at = time.time()
        except Exception as e:  # noqa: BLE001
            log.debug(f"enrich {em.market.slug} failed: {e}")

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------
    def _bucket(self, snap: MarketSnapshot) -> None:
        for em in snap.markets.values():
            # Arbitrage: both asks present, YES_ask + NO_ask < 1 - min_profit
            if em.yes_ask is not None and em.no_ask is not None:
                s = em.yes_ask + em.no_ask
                if (
                    s < (1.0 - CFG.arb_min_profit)
                    and em.market.volume_usdc >= CFG.arb_min_volume
                ):
                    snap.arbitrage_candidates.append(em)

            # Tail-end: high-confidence (> min_price) resolving soon
            days = em.days_to_resolution()
            if (
                days is not None
                and 0 < days <= CFG.tail_end_max_days
                and em.yes_ask is not None
                and em.no_ask is not None
            ):
                for token_id, price in (
                    (em.market.yes_token_id, em.yes_ask),
                    (em.market.no_token_id, em.no_ask),
                ):
                    if price >= CFG.tail_end_min_price:
                        snap.tail_end_candidates.append(em)
                        break

            # Micro-spread: cheap outcomes, wide spread, active volume
            if em.yes_bid is not None and em.yes_ask is not None:
                spread = em.yes_ask - em.yes_bid
                if (
                    CFG.micro_price_min <= em.yes_bid <= CFG.micro_price_max
                    and spread >= CFG.micro_min_spread
                    and em.market.volume_usdc >= CFG.micro_min_volume_per_min * 10
                ):
                    snap.micro_spread_candidates.append(em)

            # Crypto 15-min buckets (MM, DipArb)
            if em.is_crypto_15m():
                snap.crypto_15m_markets.append(em)

            # Sniper: any liquid market where the ask is cheap enough that
            # stacking resting GTC orders at $0.01..$0.03 is sensible.
            if em.yes_ask is not None and em.yes_ask < 0.10 and em.market.volume_usdc >= 500:
                snap.sniper_candidates.append(em)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        log.info(
            f"[green]Scanner started[/] (interval={CFG.scan_interval}s, "
            f"enrich_top={self._max_enrich})"
        )
        while True:
            try:
                await self.scan_once()
            except Exception as e:  # noqa: BLE001
                log.exception(f"scan_once crashed: {e}")
            await asyncio.sleep(CFG.scan_interval)


def _best_bid_ask(book: Optional[dict]) -> tuple[Optional[float], Optional[float]]:
    if not book:
        return None, None
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    try:
        best_bid = float(bids[0]["price"]) if bids else None
    except Exception:  # noqa: BLE001
        best_bid = None
    try:
        best_ask = float(asks[0]["price"]) if asks else None
    except Exception:  # noqa: BLE001
        best_ask = None
    return best_bid, best_ask


SCANNER: Optional[MarketScanner] = None


def get_scanner() -> MarketScanner:
    global SCANNER
    if SCANNER is None:
        SCANNER = MarketScanner()
    return SCANNER
