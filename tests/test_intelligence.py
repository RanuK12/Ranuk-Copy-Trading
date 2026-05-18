"""Tests for bot.intelligence — the guardrails module.

Covers:
  * looks_like_sports_market / is_live_sports_event
  * is_wallet_panic_selling (the Counter-Strike scenario)
  * orderbook_has_liquidity
  * risk_adjusted_edge_ok
  * position_should_force_exit (max-hold timer)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from bot.intelligence import (
    is_live_sports_event,
    is_wallet_panic_selling,
    looks_like_sports_market,
    orderbook_has_liquidity,
    position_should_force_exit,
    risk_adjusted_edge_ok,
)


# ---------------------------------------------------------------------------
# Sports / live-event detector
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slug",
    [
        "counter-strike-liquid-vs-m80-bo3",
        "arizona-diamondbacks-vs-texas-rangers",
        "nba-lakers-vs-warriors",
        "nfl-bills-vs-chiefs-q1",
        "ufc-khabib-vs-mcgregor",
        "dota2-bo5-team-spirit-vs-og",
    ],
)
def test_looks_like_sports_positive(slug):
    assert looks_like_sports_market(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "will-bitcoin-above-100k-in-2026",
        "us-election-2028",
        "strait-of-hormuz-traffic-returns-to-normal",
        "fed-rate-cut-june-2026",
    ],
)
def test_looks_like_sports_negative(slug):
    assert not looks_like_sports_market(slug)


def _end_date_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def test_live_sports_event_flags_wide_spread_game():
    """Counter-Strike mid-game: short window + wide spread → live."""
    live, reason = is_live_sports_event(
        slug="counter-strike-liquid-vs-m80-bo3",
        question="Will Liquid beat M80?",
        end_date=_end_date_in(2),
        yes_ask=0.80,
        yes_bid=0.20,  # spread 60¢ — clearly in-game
        no_ask=0.20,
    )
    assert live is True
    assert "sports-wide-spread" in reason


def test_live_sports_event_flags_decided_game():
    """One side under 2¢ → event already effectively decided."""
    live, reason = is_live_sports_event(
        slug="nba-lakers-vs-warriors",
        question="",
        end_date=_end_date_in(0.5),
        yes_ask=0.005,
        yes_bid=0.001,
        no_ask=0.999,
    )
    assert live is True
    assert "sports-decided" in reason


def test_live_sports_event_passes_upcoming_game_with_tight_book():
    """Same game 5h out with a tight book → pre-game, safe to consider."""
    live, _ = is_live_sports_event(
        slug="arizona-diamondbacks-vs-texas-rangers",
        question="",
        end_date=_end_date_in(5),
        yes_ask=0.52,
        yes_bid=0.48,  # 4¢ spread
        no_ask=0.48,
        live_window_hours=3.0,
    )
    assert live is False


def test_live_sports_event_ignores_non_sports():
    """Non-sports markets are never flagged as "live game"."""
    live, _ = is_live_sports_event(
        slug="fed-rate-cut-june-2026",
        question="Will the Fed cut rates in June?",
        end_date=_end_date_in(1),
        yes_ask=0.80,
        yes_bid=0.20,
        no_ask=0.20,
    )
    assert live is False


# ---------------------------------------------------------------------------
# Panic-sell detector
# ---------------------------------------------------------------------------

def _trade(side, price, asset="tok-A", age_s=0):
    return {
        "side": side,
        "price": price,
        "size": 100.0,
        "asset": asset,
        "timestamp": int(time.time()) - age_s,
        "transactionHash": f"0x{asset}-{side}-{price}",
    }


def test_panic_dump_detected_when_selling_below_buys():
    """Wallet bought at 0.50 then dumped three times at 0.05 → panic."""
    trades = [
        _trade("BUY", 0.50, age_s=1800),
        _trade("BUY", 0.45, age_s=1700),
        _trade("SELL", 0.08, age_s=120),
        _trade("SELL", 0.06, age_s=60),
        _trade("SELL", 0.04, age_s=10),
    ]
    panic, reason = is_wallet_panic_selling(
        trades, lookback_seconds=3600, min_sells=3, price_drop_pct=0.30
    )
    assert panic is True
    assert "panic-dump" in reason or "sell-only-streak" in reason


def test_panic_sell_only_streak():
    """No recent buys, repeated falling sells → panic."""
    trades = [
        _trade("SELL", 0.30, age_s=200),
        _trade("SELL", 0.20, age_s=150),
        _trade("SELL", 0.10, age_s=100),
        _trade("SELL", 0.05, age_s=50),
    ]
    panic, _ = is_wallet_panic_selling(
        trades, lookback_seconds=3600, min_sells=3, price_drop_pct=0.30
    )
    assert panic is True


def test_normal_trading_is_not_panic():
    """Wallet doing normal mixed BUY/SELL at similar prices → NOT panic."""
    trades = [
        _trade("BUY", 0.30, asset="a", age_s=600),
        _trade("SELL", 0.35, asset="a", age_s=100),
        _trade("BUY", 0.45, asset="b", age_s=500),
        _trade("SELL", 0.50, asset="b", age_s=80),
    ]
    panic, _ = is_wallet_panic_selling(
        trades, lookback_seconds=3600, min_sells=3, price_drop_pct=0.30
    )
    assert panic is False


def test_empty_trades_not_panic():
    panic, _ = is_wallet_panic_selling([])
    assert panic is False


# ---------------------------------------------------------------------------
# Orderbook liquidity
# ---------------------------------------------------------------------------

def test_liquidity_accepts_deep_tight_book():
    book = {
        "bids": [
            {"price": 0.90, "size": 50},
            {"price": 0.89, "size": 100},
        ],
        "asks": [
            {"price": 0.92, "size": 40},
        ],
    }
    ok, _ = orderbook_has_liquidity(
        book, desired_size_usdc=2.0, side="BUY",
        max_spread=0.05, min_depth_multiplier=1.5,
    )
    assert ok


def test_liquidity_rejects_wide_spread():
    book = {
        "bids": [{"price": 0.40, "size": 1000}],
        "asks": [{"price": 0.80, "size": 1000}],
    }
    ok, reason = orderbook_has_liquidity(
        book, desired_size_usdc=2.0, side="BUY"
    )
    assert not ok
    assert "spread-too-wide" in reason


def test_liquidity_rejects_thin_exit_side():
    book = {
        "bids": [{"price": 0.90, "size": 0.5}],  # $0.45 total bid depth
        "asks": [{"price": 0.92, "size": 10}],
    }
    ok, reason = orderbook_has_liquidity(
        book, desired_size_usdc=2.0, side="BUY"
    )
    assert not ok
    assert "depth-too-thin" in reason


def test_liquidity_rejects_empty_book():
    ok, _ = orderbook_has_liquidity(
        {"bids": [], "asks": []}, desired_size_usdc=2.0, side="BUY"
    )
    assert not ok


# ---------------------------------------------------------------------------
# Risk-adjusted edge
# ---------------------------------------------------------------------------

def test_risk_adjusted_edge_accepts_good_ratio():
    # Buy at 0.90, SL at 0.82 → upside 0.10 / downside 0.08 → ratio 1.25
    ok, reason = risk_adjusted_edge_ok(
        entry_price=0.90, stop_loss_price=0.82, min_ratio=1.0
    )
    assert ok
    assert "1.25" in reason


def test_risk_adjusted_edge_rejects_bad_ratio():
    # Buy at 0.92, SL at 0.72 → upside 0.08 / downside 0.20 → ratio 0.40
    ok, reason = risk_adjusted_edge_ok(
        entry_price=0.92, stop_loss_price=0.72, min_ratio=1.0
    )
    assert not ok
    assert "0.40" in reason


def test_risk_adjusted_edge_no_downside_is_ok():
    """If SL >= entry the downside is ~0 so accept regardless of ratio."""
    ok, _ = risk_adjusted_edge_ok(
        entry_price=0.90, stop_loss_price=0.95, min_ratio=1.0
    )
    assert ok


# ---------------------------------------------------------------------------
# Max-hold timer
# ---------------------------------------------------------------------------

def test_max_hold_triggers_after_threshold():
    opened = time.time() - (13 * 3600)  # 13 hours ago
    assert position_should_force_exit(
        opened_at=opened, max_hold_seconds=12 * 3600
    ) is True


def test_max_hold_does_not_trigger_early():
    opened = time.time() - (6 * 3600)  # 6 hours ago
    assert position_should_force_exit(
        opened_at=opened, max_hold_seconds=12 * 3600
    ) is False


def test_max_hold_zero_disables():
    opened = time.time() - (100 * 3600)
    assert position_should_force_exit(
        opened_at=opened, max_hold_seconds=0
    ) is False


def test_max_hold_none_opened_at_returns_false():
    assert position_should_force_exit(
        opened_at=None, max_hold_seconds=12 * 3600
    ) is False
