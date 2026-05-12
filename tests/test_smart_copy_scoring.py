"""Tests for the wallet-scoring function used by SmartCopyStrategy.

The function operates on raw ``/trades`` rows from the Polymarket Data API
so we feed in hand-crafted inputs.
"""

from __future__ import annotations

from bot.strategies.smart_copy import _score_from_trades


def _trade(
    side: str,
    price: float,
    size: float,
    ts: int,
    asset: str = "A1",
) -> dict:
    return {
        "side": side,
        "price": price,
        "size": size,
        "timestamp": ts,
        "asset": asset,
        "conditionId": "0xCOND",
    }


def test_empty_trades_fail_all_filters():
    score = _score_from_trades([])
    assert score.win_rate == 0
    assert score.profit_factor == 0
    assert score.total_pnl == 0


def test_pure_winning_wallet():
    # Three buy/sell pairs, each +0.10 per share, 10 shares -> +1 each round trip
    trades = []
    for i in range(3):
        trades.append(_trade("BUY", 0.40, 10, 1000 + i * 100, asset=f"A{i}"))
        trades.append(_trade("SELL", 0.50, 10, 1000 + i * 100 + 10, asset=f"A{i}"))
    score = _score_from_trades(trades)
    assert score.win_rate == 1.0
    assert score.total_pnl > 0
    # No losses -> pf capped at 99
    assert score.profit_factor == 99.0


def test_mixed_wallet_computes_profit_factor():
    trades = [
        # Win: +0.15 * 10 = +1.50
        _trade("BUY", 0.40, 10, 1000, asset="A"),
        _trade("SELL", 0.55, 10, 1001, asset="A"),
        # Loss: -0.10 * 10 = -1.00
        _trade("BUY", 0.60, 10, 1100, asset="B"),
        _trade("SELL", 0.50, 10, 1101, asset="B"),
    ]
    score = _score_from_trades(trades)
    assert score.win_rate == 0.5
    assert 1.4 < score.profit_factor < 1.6  # 1.5 / 1.0


def test_consistency_across_weeks():
    # Two weeks: week 1 all winners, week 2 all losers -> consistency 0.5
    trades = []
    # Week 1 (2024-W01)
    trades += [
        _trade("BUY", 0.40, 5, 1704067200, asset="A"),
        _trade("SELL", 0.50, 5, 1704067300, asset="A"),
    ]
    # Week 2 (2024-W02)
    trades += [
        _trade("BUY", 0.60, 5, 1704672000, asset="B"),
        _trade("SELL", 0.50, 5, 1704672100, asset="B"),
    ]
    score = _score_from_trades(trades)
    assert score.consistency == 0.5
