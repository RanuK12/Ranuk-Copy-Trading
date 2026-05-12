"""Strategy D — DipArb on 15-minute crypto markets.

Detects panic selling on Polymarket BTC/ETH/SOL 15-min markets and
fades the move *only* when the corresponding Binance spot market is
NOT making the same move (CEX confirmation filter). This filters out
true crashes from thin-book anomalies.

Triggers
--------
* Polymarket side drops >= ``DIP_MIN_DROP`` (default 15%) in
  ``DIP_LOOKBACK_SECONDS`` (default 3s).
* Binance spot moved < 1/3 of the Polymarket drop in the same window.

On trigger the bot buys the dipped side plus places a small hedge on the
opposite side (20% of the main leg) in case the panic continues.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from typing import Deque, Iterable, Optional

from bot.clients.binance import get_binance
from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_DIP_ARB,
    Side,
)
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.base import Strategy


_ASSET_RE = re.compile(r"\b(btc|bitcoin|eth|ethereum|sol|solana)\b", re.IGNORECASE)


def _asset_of(em: EnrichedMarket) -> Optional[str]:
    text = f"{em.market.slug} {em.market.question}".lower()
    m = _ASSET_RE.search(text)
    if not m:
        return None
    word = m.group(1).lower()
    return {"bitcoin": "btc", "ethereum": "eth", "solana": "sol"}.get(word, word)


class DipArbStrategy(Strategy):
    name = "dip_arb"

    def __init__(self) -> None:
        super().__init__()
        # Per-market rolling window of (timestamp, yes_mid).
        self._history: dict[str, Deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=32)
        )
        self._binance = get_binance()

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        now = time.time()
        out: list[Opportunity] = []

        for em in snap.crypto_15m_markets:
            if em.yes_mid is None:
                continue
            hist = self._history[em.market.condition_id]
            hist.append((now, em.yes_mid))

            # Compute the min mid within the lookback window
            cutoff = now - CFG.dip_lookback_seconds
            prior = [p for (t, p) in hist if t <= cutoff]
            if not prior:
                continue
            ref = max(prior)  # highest recent price before the dip
            drop = (ref - em.yes_mid) / max(ref, 1e-6)
            if drop < CFG.dip_min_drop:
                continue

            asset = _asset_of(em)
            if asset is None:
                continue

            binance_move = await self._binance.pct_move_last_seconds(
                asset, max(3, CFG.dip_lookback_seconds)
            )
            if binance_move is None:
                self.log.debug(f"dip_arb: no binance data for {asset}; skipping.")
                continue

            # Binance confirmation: only fade if CEX did NOT move as much.
            if abs(binance_move) >= drop / 3:
                self.log.info(
                    f"dip_arb: rejecting {em.market.slug} — binance moved "
                    f"{binance_move*100:.2f}% vs polymarket drop {drop*100:.2f}%."
                )
                continue

            out.append(self._build_opp(em, drop, binance_move))

        return out

    def _build_opp(
        self, em: EnrichedMarket, drop: float, binance_move: float
    ) -> Opportunity:
        primary_size = self.size_usdc()
        hedge_size = round(primary_size * 0.20, 4)
        # Primary: fade the dip on the side that dropped (we track yes_mid).
        primary = Leg(
            token_id=em.market.yes_token_id,
            side=Side.BUY,
            size_usdc=primary_size,
            kind=OrderKind.FOK,
            limit_price=em.yes_ask or em.yes_mid,
        )
        hedge = Leg(
            token_id=em.market.no_token_id,
            side=Side.BUY,
            size_usdc=hedge_size,
            kind=OrderKind.LIMIT,
            limit_price=em.no_ask or em.no_mid,
        )
        return Opportunity(
            strategy=self.name,
            market_id=em.market.condition_id,
            market_slug=em.market.slug,
            priority=PRIORITY_DIP_ARB,
            confidence=0.72,
            expected_profit_pct=drop / 2,  # expect to capture half the reversion
            legs=[primary, hedge],
            reference_price=em.yes_mid,
            max_slippage=CFG.max_slippage,
            metadata={
                "dip_pct": drop,
                "binance_move_pct": binance_move,
                "primary_size": primary_size,
                "hedge_size": hedge_size,
            },
        )
