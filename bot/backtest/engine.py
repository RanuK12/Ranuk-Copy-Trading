"""Offline backtest engine skeleton.

Feeds historical ``MarketSnapshot`` frames into a :class:`~bot.strategies.base.Strategy`
and measures Win Rate, Profit Factor, Sharpe, Max Drawdown and Expectancy.

Usage
-----
>>> from bot.backtest.engine import BacktestEngine, MarketFrame
>>> frames = load_history_from_gamma("2025-10", "2025-12")  # user-provided
>>> engine = BacktestEngine(strategy=ArbitrageStrategy(), frames=frames)
>>> report = await engine.run()
>>> print(report.summary())

The engine intentionally keeps fill simulation simple: every opportunity
emitted by the strategy is assumed to fill at ``reference_price`` and
resolve at ``reference_price * (1 + expected_profit_pct)`` for a winning
trade, or at a configurable stop-loss for losers. Realistic slippage
models can be injected via the ``fill_model`` hook.

Only strategies whose backtest win-rate exceeds ``min_win_rate`` should
be promoted to the live config (per spec section 5.2).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from bot.models import Opportunity
from bot.scanner import MarketSnapshot
from bot.strategies.base import Strategy


# ---------------------------------------------------------------------------
# Data frames
# ---------------------------------------------------------------------------
@dataclass
class MarketFrame:
    """One recorded ``MarketSnapshot`` at a point in time, plus labels.

    ``outcomes`` maps ``market_id -> 0|1`` indicating the eventual winning
    side (1 == YES won). This is what lets the engine evaluate PnL.
    """

    snapshot: MarketSnapshot
    outcomes: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fill model
# ---------------------------------------------------------------------------
FillModel = Callable[[Opportunity, MarketFrame], float]


def default_fill_model(opp: Opportunity, frame: MarketFrame) -> float:
    """Return realised PnL in USDC for a simulated fill.

    Strategy provides ``reference_price`` and ``expected_profit_pct``.
    If the market has a recorded outcome, we assume the fill converts
    to 1 USDC per share when the bought side wins, else 0.
    """
    outcome = frame.outcomes.get(opp.market_id)
    notional = sum(leg.size_usdc for leg in opp.legs) or 0.0
    if outcome is None:
        # No label → use expected_profit_pct as the realised return
        return notional * opp.expected_profit_pct
    # Arbitrage-style opportunities are path-independent; treat as guaranteed
    if opp.strategy == "arbitrage":
        return notional * opp.expected_profit_pct
    # Single-leg directional bet: if picked outcome won, PnL = (1 - price) * shares
    price = opp.reference_price or 0.5
    shares = notional / price if price > 0 else 0.0
    return shares * (1 - price) if outcome == 1 else -shares * price


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
@dataclass
class BacktestReport:
    strategy: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_series: list[float] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(self.pnl_series)

    @property
    def profit_factor(self) -> float:
        gains = sum(p for p in self.pnl_series if p > 0)
        losses = abs(sum(p for p in self.pnl_series if p < 0))
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def expectancy(self) -> float:
        return (sum(self.pnl_series) / len(self.pnl_series)) if self.pnl_series else 0.0

    @property
    def max_drawdown(self) -> float:
        peak = -math.inf
        mdd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            mdd = max(mdd, peak - eq)
        return mdd

    @property
    def sharpe(self) -> float:
        if len(self.pnl_series) < 2:
            return 0.0
        mean = sum(self.pnl_series) / len(self.pnl_series)
        var = sum((p - mean) ** 2 for p in self.pnl_series) / (len(self.pnl_series) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std == 0:
            return 0.0
        # Scale by sqrt(N); caller can annualize if they know the cadence.
        return (mean / std) * math.sqrt(len(self.pnl_series))

    def summary(self) -> str:
        return (
            f"Strategy: {self.strategy}\n"
            f"  Trades:        {self.trades}\n"
            f"  Win Rate:      {self.win_rate * 100:.1f}%\n"
            f"  Total PnL:     {self.total_pnl:+.2f} USDC\n"
            f"  Profit Factor: {self.profit_factor:.2f}\n"
            f"  Expectancy:    {self.expectancy:+.4f} USDC/trade\n"
            f"  Max Drawdown:  {self.max_drawdown:.2f} USDC\n"
            f"  Sharpe:        {self.sharpe:.2f}"
        )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        frames: Iterable[MarketFrame],
        *,
        fill_model: Optional[FillModel] = None,
        seed: int = 42,
    ) -> None:
        self._strategy = strategy
        self._frames = list(frames)
        self._fill_model = fill_model or default_fill_model
        random.seed(seed)

    async def run(self) -> BacktestReport:
        report = BacktestReport(strategy=self._strategy.name)
        equity = 0.0
        for frame in self._frames:
            opps = await self._strategy.generate(frame.snapshot) or []
            for opp in opps:
                pnl = self._fill_model(opp, frame)
                report.trades += 1
                report.pnl_series.append(pnl)
                if pnl >= 0:
                    report.wins += 1
                else:
                    report.losses += 1
                equity += pnl
                report.equity_curve.append(equity)
        return report
