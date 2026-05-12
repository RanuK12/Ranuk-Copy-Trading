"""Strategy E — Smart Copy-Trading (elite filters, not blind copy).

Monitors a curated list of proxy wallets from the Polymarket leaderboard,
but only replicates trades from wallets that pass a strict performance
screen:

* Win rate          >= ``COPY_MIN_WIN_RATE``  (default 60%)
* Profit factor     >= ``COPY_MIN_PROFIT_FACTOR`` (default 1.5x)
* Total realized PnL >= ``COPY_MIN_TOTAL_PNL_USDC`` (default 500 USDC)
* Consistency (share of profitable weeks) >= ``COPY_MIN_CONSISTENCY`` (0.70)
* Largest single trade <= ``COPY_MAX_SINGLE_TRADE_PCT`` of PnL (30%)

The screen is evaluated against each wallet's ``/trades`` feed from the
Polymarket Data API. Wallet scoring is cached for 30 minutes so the
scanner's 5-second cadence does not hammer the Data API.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from bot.clients.polymarket import get_poly
from bot.config import CFG
from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_SMART_COPY,
    Side,
)
from bot.scanner import MarketSnapshot
from bot.state import get_state
from bot.strategies.base import Strategy


_SCORE_TTL_SECONDS = 30 * 60


@dataclass
class WalletScore:
    win_rate: float
    profit_factor: float
    total_pnl: float
    consistency: float
    max_single_trade_pct: float
    passes: bool
    computed_at: float


class SmartCopyStrategy(Strategy):
    name = "smart_copy"

    def __init__(self) -> None:
        super().__init__()
        self._poly = get_poly()
        self._state = get_state()
        self._scores: dict[str, WalletScore] = {}

    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        if not CFG.smart_wallets:
            return []

        opps: list[Opportunity] = []
        for wallet in CFG.smart_wallets:
            score = await self._score_wallet(wallet)
            if not score.passes:
                self.log.debug(
                    f"wallet {wallet[:10]}... filtered "
                    f"wr={score.win_rate:.2f} pf={score.profit_factor:.2f} "
                    f"pnl={score.total_pnl:.2f} cons={score.consistency:.2f}"
                )
                continue

            trades = await self._poly.get_user_trades_async(wallet, limit=10)
            last_seen = self._state.get_last_seen_tx(wallet)

            # Walk in chronological order, stopping at the previously seen tx.
            new_trades = []
            for t in trades:
                if t.get("transactionHash") == last_seen:
                    break
                new_trades.append(t)

            for trade in reversed(new_trades):
                opp = self._opp_from_trade(trade, snap, wallet, score)
                if opp is not None:
                    opps.append(opp)

            if trades:
                self._state.set_last_seen_tx(
                    wallet, trades[0].get("transactionHash", last_seen or "")
                )

        return opps

    # ------------------------------------------------------------------
    async def _score_wallet(self, wallet: str) -> WalletScore:
        cached = self._scores.get(wallet)
        if cached and (time.time() - cached.computed_at) < _SCORE_TTL_SECONDS:
            return cached

        trades = await self._poly.get_user_trades_async(wallet, limit=500)
        score = _score_from_trades(trades)
        score.passes = (
            score.win_rate >= CFG.copy_min_win_rate
            and score.profit_factor >= CFG.copy_min_profit_factor
            and score.total_pnl >= CFG.copy_min_total_pnl
            and score.consistency >= CFG.copy_min_consistency
            and score.max_single_trade_pct <= CFG.copy_max_single_trade_pct
        )
        score.computed_at = time.time()
        self._scores[wallet] = score
        self.log.info(
            f"scored wallet {wallet[:10]}... "
            f"wr={score.win_rate:.2f} pf={score.profit_factor:.2f} "
            f"pnl={score.total_pnl:.0f} cons={score.consistency:.2f} "
            f"pass={score.passes}"
        )
        return score

    def _opp_from_trade(
        self,
        trade: dict,
        snap: MarketSnapshot,
        wallet: str,
        score: WalletScore,
    ) -> Optional[Opportunity]:
        side = (trade.get("side") or "").upper()
        if side != "BUY":  # only copy BUYs; spec permits toggle but default is BUYs
            return None
        condition_id = trade.get("conditionId")
        token_id = trade.get("asset")
        price = float(trade.get("price") or 0)
        if not condition_id or not token_id or price <= 0:
            return None

        em = snap.markets.get(condition_id)
        size = self.size_usdc()
        leg = Leg(
            token_id=str(token_id),
            side=Side.BUY,
            size_usdc=size,
            kind=OrderKind.LIMIT,
            limit_price=round(price * (1 + CFG.max_slippage), 4),
        )
        return Opportunity(
            strategy=self.name,
            market_id=condition_id,
            market_slug=trade.get("slug") or condition_id,
            priority=PRIORITY_SMART_COPY,
            confidence=min(0.95, score.win_rate),
            expected_profit_pct=0.05,  # conservative
            legs=[leg],
            reference_price=price,
            max_slippage=CFG.max_slippage,
            metadata={
                "source_wallet": wallet,
                "detected_price": price,
                "side": side,
                "market_volume": em.market.volume_usdc if em else None,
                "wallet_win_rate": score.win_rate,
                "wallet_profit_factor": score.profit_factor,
            },
        )


# ---------------------------------------------------------------------------
# Wallet scoring (pure function -> exposed for tests/backtest)
# ---------------------------------------------------------------------------
def _score_from_trades(trades: list[dict]) -> WalletScore:
    """Compute a WalletScore from a list of Data-API /trades rows.

    This is conservative by design:
    * win/loss is approximated from the side + price delta on the same asset.
    * profit factor uses sum of positive PnL / |sum of negative PnL|.
    * consistency = share of ISO-weeks with net positive PnL.
    """
    if not trades:
        return WalletScore(0, 0, 0, 0, 1.0, False, time.time())

    # Group consecutive trades on the same asset to approximate a round-trip.
    by_asset: dict[str, list[dict]] = defaultdict(list)
    for t in sorted(trades, key=lambda x: x.get("timestamp", 0)):
        by_asset[str(t.get("asset"))].append(t)

    per_trade_pnl: list[tuple[int, float]] = []  # (timestamp, pnl)
    for _, ts in by_asset.items():
        entry_price: Optional[float] = None
        entry_size: float = 0.0
        for t in ts:
            price = float(t.get("price") or 0)
            size = float(t.get("size") or 0)
            side = (t.get("side") or "").upper()
            timestamp = int(t.get("timestamp") or 0)
            if side == "BUY":
                entry_price = price
                entry_size = size
            elif side == "SELL" and entry_price is not None and entry_size > 0:
                filled = min(entry_size, size)
                pnl = (price - entry_price) * filled
                per_trade_pnl.append((timestamp, pnl))
                entry_size -= filled
                if entry_size <= 0:
                    entry_price = None
                    entry_size = 0.0

    if not per_trade_pnl:
        return WalletScore(0, 0, 0, 0, 1.0, False, time.time())

    wins = sum(1 for _, p in per_trade_pnl if p > 0)
    losses = sum(1 for _, p in per_trade_pnl if p < 0)
    total = len(per_trade_pnl)
    total_pnl = sum(p for _, p in per_trade_pnl)
    pos_sum = sum(p for _, p in per_trade_pnl if p > 0)
    neg_sum = abs(sum(p for _, p in per_trade_pnl if p < 0))

    win_rate = wins / total
    profit_factor = (pos_sum / neg_sum) if neg_sum > 0 else float("inf") if wins > 0 else 0.0

    # Consistency: share of ISO weeks with positive net PnL.
    weeks: dict[str, float] = defaultdict(float)
    for ts, pnl in per_trade_pnl:
        key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%G-W%V")
        weeks[key] += pnl
    consistency = (
        sum(1 for v in weeks.values() if v > 0) / len(weeks) if weeks else 0.0
    )

    max_single = max((abs(p) for _, p in per_trade_pnl), default=0.0)
    max_single_pct = (max_single / total_pnl) if total_pnl > 0 else 1.0

    # Cap profit_factor so downstream comparisons don't propagate inf
    if profit_factor == float("inf"):
        profit_factor = 99.0

    return WalletScore(
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_pnl=total_pnl,
        consistency=consistency,
        max_single_trade_pct=max_single_pct,
        passes=False,
        computed_at=time.time(),
    )
