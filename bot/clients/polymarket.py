"""Polymarket REST + CLOB client wrapper.

Combines three upstream APIs:

* Gamma API — market discovery (active markets, slug, clobTokenIds).
* Data API  — public user trades / activity (smart-money wallets).
* CLOB API  — orderbook, prices, and authenticated order placement via
  ``py-clob-client-v2``.

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
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=80
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
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
        """Return active, non-closed binary markets with token IDs populated.

        Ordered by ``volume24hr`` descending so the most liquid markets are
        included even when the API caps the response at 500 rows.
        """
        markets_raw = self._get(
            f"{CFG.gamma_api_host}/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
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

    def fetch_sports_markets(self, limit: int = 200) -> list[PolyMarket]:
        """Return active sports markets (short-term games) via Gamma tag_id=100639.

        Ordered by ``volume24hr`` descending so the hottest games come first.
        """
        markets_raw = self._get(
            f"{CFG.gamma_api_host}/markets",
            {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "tag_id": "100639",
                "order": "volume24hr",
                "ascending": "false",
            },
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
                log.debug(f"Skipping malformed sports market {m.get('conditionId')}: {e}")
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
    # Balance / portfolio value (CLOB + data-api hybrid)
    # ------------------------------------------------------------------
    def get_usdc_available(self, wallet: str) -> Optional[float]:
        """Return the USDC balance available to place orders, via the CLOB client.

        Falls back to ``None`` if the CLOB client isn't initialised or the
        balance endpoint is unreachable. Uses USDC collateral balance.
        """
        try:
            client = self.clob()
            if client is None:
                return None
            # py-clob-client-v2: BalanceAllowanceParams with collateral asset type
            try:
                from py_clob_client_v2.clob_types import (  # type: ignore
                    BalanceAllowanceParams,
                    AssetType,
                )

                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    token_id="",
                    signature_type=CFG.poly_signature_type,
                )
                resp = client.get_balance_allowance(params)
                # Balance is returned as integer USDC (6 decimals).
                bal_raw = resp.get("balance") if isinstance(resp, dict) else None
                if bal_raw is None:
                    return None
                return float(bal_raw) / 1_000_000.0
            except Exception as e:  # noqa: BLE001
                log.debug(f"get_usdc_available CLOB call failed: {e}")
                return None
        except Exception as e:  # noqa: BLE001
            log.debug(f"get_usdc_available top-level failed: {e}")
            return None

    def get_positions_value(self, wallet: str) -> float:
        """Sum of size * curPrice across the wallet's open positions."""
        try:
            val = self._get(
                f"{CFG.data_api_host}/value",
                {"user": wallet},
            )
            if isinstance(val, list) and val:
                return float(val[0].get("value") or 0)
        except Exception as e:  # noqa: BLE001
            log.debug(f"positions_value failed: {e}")
        # Fallback: iterate positions endpoint.
        try:
            total = 0.0
            for p in self.get_user_positions(wallet):
                total += float(p.get("size") or 0) * float(p.get("curPrice") or 0)
            return total
        except Exception:  # noqa: BLE001
            return 0.0

    def get_portfolio_value(self, wallet: str) -> Optional[float]:
        """Total portfolio value = available USDC + value of open positions.

        Returns ``None`` if neither number could be resolved.
        """
        usdc = self.get_usdc_available(wallet)
        pos = self.get_positions_value(wallet)
        if usdc is None and pos == 0:
            return None
        return (usdc or 0.0) + pos

    # ------------------------------------------------------------------
    # CLOB authenticated client (py-clob-client-v2) — live mode only
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

        from py_clob_client_v2 import ClobClient, ApiCreds  # type: ignore
        from py_clob_client_v2.order_utils import SignatureTypeV2  # type: ignore

        from bot.clients.clob_v2_auth import get_or_create_credentials  # type: ignore

        creds_dict = get_or_create_credentials(
            CFG.poly_private_key, host=CFG.clob_host
        )
        creds = ApiCreds(
            api_key=creds_dict["api_key"],
            api_secret=creds_dict["api_secret"],
            api_passphrase=creds_dict["api_passphrase"],
        )

        client = ClobClient(
            host=CFG.clob_host,
            chain_id=137,
            key=CFG.poly_private_key,
            signature_type=SignatureTypeV2.POLY_1271,
            funder=CFG.poly_funder,
            creds=creds,
        )
        log.info("[green]CLOB V2 client authenticated.[/]")
        self._clob = client
        return client

    # ------------------------------------------------------------------
    # Sell — market order to exit a position
    # ------------------------------------------------------------------
    def sell_position(self, token_id: str, shares: float) -> dict[str, Any]:
        """Place a FOK market sell order for the given shares amount."""
        try:
            from py_clob_client_v2.clob_types import MarketOrderArgs, OrderType  # type: ignore
        except Exception as e:
            return {"success": False, "error": f"py-clob-client-v2 unavailable: {e}"}

        client = self.clob()
        if client is None:
            return {"success": False, "error": "CLOB client not initialized"}

        try:
            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(round(shares, 2)),
                side="SELL",
                order_type=OrderType.FOK,
            )
            resp = client.create_and_post_market_order(args, order_type=OrderType.FOK)
            return {"success": True, "response": resp}
        except Exception as e:
            log.warning(f"[red]SELL order failed[/] token={token_id[:16]}... shares={shares:.2f} -> {e}")
            return {"success": False, "error": str(e)}

    async def sell_position_async(self, token_id: str, shares: float) -> dict[str, Any]:
        return await asyncio.to_thread(self.sell_position, token_id, shares)

    # Simple async wrappers (delegate to threads so we don't block the loop)
    async def get_price_async(self, token_id: str, side: str = "BUY") -> Optional[float]:
        return await asyncio.to_thread(self.get_price, token_id, side)

    async def get_order_book_async(self, token_id: str) -> Optional[dict]:
        return await asyncio.to_thread(self.get_order_book, token_id)

    async def fetch_active_markets_async(self, limit: int = 500) -> list[PolyMarket]:
        return await asyncio.to_thread(self.fetch_active_markets, limit)

    async def fetch_sports_markets_async(self, limit: int = 200) -> list[PolyMarket]:
        return await asyncio.to_thread(self.fetch_sports_markets, limit)

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
