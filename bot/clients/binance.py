"""Minimal Binance spot-price client used by the DipArb strategy.

The DipArb strategy requires a *CEX confirmation* before chasing a dip on
Polymarket's 15-minute crypto markets: if Binance BTC is flat but the
Polymarket BTC market dumps 15% in 3 seconds, the dump is almost certainly
a thin-book anomaly worth fading.

This is a read-only public endpoint (no API key needed).
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

import httpx

from bot.logger import get_logger

log = get_logger("binance")


SYMBOL_MAP = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
}


class BinanceClient:
    BASE = "https://api.binance.com"

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=5.0)
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, ts)
        self._cache_ttl = 1.0  # seconds

    async def get_price(self, asset: str) -> Optional[float]:
        symbol = SYMBOL_MAP.get(asset.lower()) or asset.upper()
        cached = self._cache.get(symbol)
        if cached and (time.time() - cached[1]) < self._cache_ttl:
            return cached[0]
        try:
            r = await self._http.get(
                f"{self.BASE}/api/v3/ticker/price", params={"symbol": symbol}
            )
            r.raise_for_status()
            price = float(r.json()["price"])
            self._cache[symbol] = (price, time.time())
            return price
        except Exception as e:  # noqa: BLE001
            log.debug(f"binance ticker {symbol} failed: {e}")
            return None

    async def pct_move_last_seconds(
        self, asset: str, lookback_seconds: int
    ) -> Optional[float]:
        """Return the % change between ``t-lookback`` and now from klines."""
        symbol = SYMBOL_MAP.get(asset.lower()) or asset.upper()
        try:
            r = await self._http.get(
                f"{self.BASE}/api/v3/klines",
                params={"symbol": symbol, "interval": "1s", "limit": lookback_seconds + 1},
            )
            r.raise_for_status()
            klines = r.json()
            if len(klines) < 2:
                return None
            first_close = float(klines[0][4])
            last_close = float(klines[-1][4])
            if first_close == 0:
                return None
            return (last_close - first_close) / first_close
        except Exception:  # noqa: BLE001
            return None

    async def close(self) -> None:
        await self._http.aclose()


BN: Optional[BinanceClient] = None


def get_binance() -> BinanceClient:
    global BN
    if BN is None:
        BN = BinanceClient()
    return BN


# Synchronous helper for tests
def sync_get_price(asset: str) -> Optional[float]:
    return asyncio.run(get_binance().get_price(asset))
