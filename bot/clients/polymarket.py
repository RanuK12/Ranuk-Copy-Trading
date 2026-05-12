"""Polymarket REST + CLOB client wrapper.

Combines three upstream APIs:

* Gamma API — market discovery (active markets, slug, clobTokenIds).
* Data API  — public user trades / activity (smart-money wallets).
* CLOB API  — orderbook, prices, and authenticated order placement via
  ``py-clob-client``.

A shared ``requests.Session`` is used for connection pooling, and a simple
token-bucket rate limiter caps outbound requests at ``POLY_RATE_LIMIT_PER_MIN``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from bot.config import CFG
from bot.logger import get_logger

log = get_logger("polymarket")


# ---------------------------------------------------------------------------
# Token-bucket rate limiter (thread-safe, async-friendly)
# ---------------------------------------------------------------------------
class _RateLimiter:
    def __init__(self, rate_per_minute: int) -> None:
        self._capacity = max(1, rate_per_minute)
        self._tokens = float(self._capacity)
        self._rate_per_sec = self._capacity / 60.0
        self._lock = threading.Lock()
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        delta = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + delta * self._rate_per_sec)
        self._last_refill = now

    def acquire(self) -> None:
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = max(0.01, (1.0 - self._tokens) / self._rate_per_sec)
            time.sleep(wait)


@dataclass
class PolyMarket:
    """Flat view of a Gamma API market, enough for strategy evaluation."""

    condition_id: str
    slug: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    volume_usdc: float = 0.0
    end_date: Optional[str] = None
    active: bool = True
    closed: bool = False
    negative_risk: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


class PolymarketClient:
    """Read helpers against Gamma / Data / CLOB plus authenticated orders."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._limiter = _RateLimiter(CFG.poly_rate_limit_per_min)
        self._clob: Any = None  # Lazily initialized (live mode only)

    # ------------------------------------------------------------------
    # Raw HTTP helpers (with rate limiting + exponential backoff)
    # ------------------------------------------------------------------
    def _get(self, url: str, params: Optional[dict] = None, *, attempts: int = 3) -> Any:
        self._limiter.acquire()
        delay = 0.5
        for i in range(attempts):
            try:
                r = self._session.get(url, params=params, timeout=8)
                r.raise_for_status()
                return r.json()
            except Exception as e:  # noqa: BLE001
                if i == attempts - 1:
                    raise
                log.debug(f"GET {url} failed ({e}); retry in {delay:.1f}s.")
                time.sleep(delay)
                delay *= 2

    # ------------------------------------------------------------------
    # Gamma — market discovery
    # ------------------------------------------------------------------
    def fetch_active_markets(self, limit: int = 500) -> list[PolyMarket]:
        """Return active, non-closed binary markets with token IDs populated."""
        markets_raw = self._get(
            f"{CFG.gamma_api_host}/markets",
            {"active": "true", "closed": "false", "limit": limit},
        )
        out: list[PolyMarket] = []
        for m in markets_raw or []:
            try:
                token_ids = m.get("clobTokenIds")
                if isinstance(token_ids, str):
                    token_ids = json.loads(token_ids)
                if not token_ids or len(token_ids) < 2:
                    continue
                out.append(
                    PolyMarket(
                        condition_id=m["conditionId"],
                        slug=m.get("slug", ""),
                        question=m.get("question", ""),
                        yes_token_id=str(token_ids[0]),
                        no_token_id=str(token_ids[1]),
                        volume_usdc=float(m.get("volume", 0) or 0),
                        end_date=m.get("endDate"),
                        active=bool(m.get("active", True)),
                        closed=bool(m.get("closed", False)),
                        negative_risk=bool(m.get("negRisk", False)),
                        raw=m,
                    )
                )
            except Exception as e:  # noqa: BLE001
                log.debug(f"Skipping malformed market {m.get('conditionId')}: {e}")
        return out

    # ------------------------------------------------------------------
    # CLOB — prices / orderbook
    # ------------------------------------------------------------------
    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        try:
            data = self._get(
                f"{CFG.clob_host}/price",
                {"token_id": token_id, "side": side.upper()},
            )
            return float(data.get("price")) if data else None
        except Exception as e:  # noqa: BLE001
            log.debug(f"price({token_id[:8]}..., {side}) failed: {e}")
            return None

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            data = self._get(f"{CFG.clob_host}/midpoint", {"token_id": token_id})
            return float(data.get("mid")) if data else None
        except Exception as e:  # noqa: BLE001
            log.debug(f"midpoint failed: {e}")
            return None

    def get_order_book(self, token_id: str) -> Optional[dict]:
        try:
            return self._get(f"{CFG.clob_host}/book", {"token_id": token_id})
        except Exception as e:  # noqa: BLE001
            log.debug(f"book failed: {e}")
            return None

    def get_best_bid_ask(self, token_id: str) -> tuple[Optional[float], Optional[float]]:
        """Convenience: (best_bid, best_ask) in dollars."""
        book = self.get_order_book(token_id)
        if not book:
            return None, None
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask = float(asks[0]["price"]) if asks else None
        return best_bid, best_ask

    # ------------------------------------------------------------------
    # Data API — user trades & activity (smart-money)
    # ------------------------------------------------------------------
    def get_user_trades(self, wallet: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            return self._get(
                f"{CFG.data_api_host}/trades",
                {"user": wallet, "limit": limit},
            ) or []
        except Exception as e:  # noqa: BLE001
            log.debug(f"user_trades({wallet[:8]}..) failed: {e}")
            return []

    def get_user_positions(self, wallet: str) -> list[dict[str, Any]]:
        try:
            return self._get(
                f"{CFG.data_api_host}/positions",
                {"user": wallet, "sizeThreshold": 1.0},
            ) or []
        except Exception as e:  # noqa: BLE001
            log.debug(f"user_positions({wallet[:8]}..) failed: {e}")
            return []

    # ------------------------------------------------------------------
    # CLOB authenticated client (py-clob-client) — live mode only
    # ------------------------------------------------------------------
    def clob(self):  # type: ignore[no-untyped-def]
        if self._clob is not None:
            return self._clob
        if CFG.is_paper:
            return None
        if not CFG.poly_private_key or not CFG.poly_funder:
            raise RuntimeError(
                "POLY_PRIVATE_KEY + POLY_FUNDER are required for live trading."
            )

        from py_clob_client.client import ClobClient  # type: ignore

        client = ClobClient(
            CFG.clob_host,
            key=CFG.poly_private_key,
            chain_id=137,
            signature_type=CFG.poly_signature_type,
            funder=CFG.poly_funder,
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        log.info("[green]CLOB client authenticated.[/]")
        self._clob = client
        return client

    # Simple async wrappers (delegate to threads so we don't block the loop)
    async def get_price_async(self, token_id: str, side: str = "BUY") -> Optional[float]:
        return await asyncio.to_thread(self.get_price, token_id, side)

    async def get_order_book_async(self, token_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self.get_order_book, token_id)

    async def fetch_active_markets_async(self, limit: int = 500) -> list[PolyMarket]:
        return await asyncio.to_thread(self.fetch_active_markets, limit)

    async def get_user_trades_async(
        self, wallet: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.get_user_trades, wallet, limit)


POLY: Optional[PolymarketClient] = None


def get_poly() -> PolymarketClient:
    global POLY
    if POLY is None:
        POLY = PolymarketClient()
    return POLY
