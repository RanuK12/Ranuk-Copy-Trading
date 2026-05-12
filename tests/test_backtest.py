"""Smoke test for the backtest engine."""

from __future__ import annotations

import pytest

from bot.backtest.engine import BacktestEngine, MarketFrame
from bot.clients.polymarket import PolyMarket
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.arbitrage import ArbitrageStrategy


def _frame(yes_ask: float, no_ask: float, outcome: int) -> MarketFrame:
    m = PolyMarket(
        condition_id=f"0x{yes_ask}-{no_ask}",
        slug="backtest-market",
        question="bt",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        volume_usdc=10_000.0,
    )
    em = EnrichedMarket(market=m, yes_ask=yes_ask, no_ask=no_ask)
    snap = MarketSnapshot(arbitrage_candidates=[em], markets={m.condition_id: em})
    return MarketFrame(snapshot=snap, outcomes={m.condition_id: outcome})


@pytest.mark.asyncio
async def test_backtest_engine_reports_metrics():
    frames = [_frame(0.45, 0.50, 1), _frame(0.40, 0.50, 0), _frame(0.48, 0.48, 1)]
    engine = BacktestEngine(strategy=ArbitrageStrategy(), frames=frames)
    report = await engine.run()
    assert report.trades == 3
    # Arbitrage always profits by construction in the default fill model
    assert report.wins == 3
    assert report.total_pnl > 0
    summary = report.summary()
    assert "Win Rate" in summary and "Profit Factor" in summary
