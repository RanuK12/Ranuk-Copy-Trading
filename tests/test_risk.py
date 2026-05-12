"""Tests for RiskManager circuit breakers and sizing."""

from __future__ import annotations

import time

from bot.config import CFG
from bot.models import Leg, Opportunity, OrderKind, Side
from bot.risk import RiskManager


def _opp(strategy: str = "arbitrage", size: float = 10.0) -> Opportunity:
    return Opportunity(
        strategy=strategy,
        market_id=f"mkt-{strategy}-{size}",
        market_slug="slug",
        legs=[
            Leg(
                token_id="tok",
                side=Side.BUY,
                size_usdc=size,
                kind=OrderKind.FOK,
            )
        ],
    )


def test_kill_switch_blocks_everything():
    rm = RiskManager()
    rm.trigger_kill_switch("unit_test")
    ok, reason = rm.allow(_opp())
    assert not ok
    assert reason == "kill_switch"


def test_market_exposure_cap():
    rm = RiskManager()
    # Cap is 5% of 1000 == $50. Two $30 trades should be allowed then blocked.
    opp1 = _opp(size=30.0)
    ok, _ = rm.allow(opp1)
    assert ok
    rm.reserve_exposure("arbitrage", opp1.market_id, 30.0)
    # Second trade on SAME market
    opp2 = Opportunity(
        strategy="arbitrage",
        market_id=opp1.market_id,
        market_slug="slug",
        legs=[Leg(token_id="t2", side=Side.BUY, size_usdc=30.0, kind=OrderKind.FOK)],
    )
    ok, reason = rm.allow(opp2)
    assert not ok
    assert reason == "market_exposure_cap"


def test_strategy_exposure_cap():
    rm = RiskManager()
    # Cap is 25% of 1000 == $250
    for i in range(8):  # 8 * 30 = 240 (ok), then next 30 breaks 250
        opp = _opp(size=30.0)
        opp.market_id = f"mkt-{i}"
        ok, _ = rm.allow(opp)
        assert ok, f"trade {i} should fit"
        rm.reserve_exposure("arbitrage", opp.market_id, 30.0)
    overflow = _opp(size=30.0)
    overflow.market_id = "mkt-overflow"
    ok, reason = rm.allow(overflow)
    assert not ok
    assert reason == "strategy_exposure_cap"


def test_daily_loss_cap_triggers_kill_switch():
    rm = RiskManager()
    # Daily cap = 5% of 1000 == $50. Register a $60 loss.
    rm.register_fill("tail_end", -60.0)
    ok, reason = rm.allow(_opp("tail_end"))
    assert not ok
    assert reason in ("daily_loss_cap", "kill_switch")
    assert rm.state.kill_switch is True


def test_consecutive_losses_pauses_strategy():
    rm = RiskManager()
    for _ in range(CFG.max_consecutive_losses):
        rm.register_fill("micro_spread", -5.0)
    # Strategy should now be paused
    paused_until = rm.state.strategy_paused_until.get("micro_spread", 0)
    assert paused_until > time.time()
    ok, reason = rm.allow(_opp("micro_spread"))
    assert not ok
    assert reason.startswith("strategy_paused")


def test_win_clears_consecutive_losses():
    rm = RiskManager()
    for _ in range(CFG.max_consecutive_losses - 1):
        rm.register_fill("tail_end", -5.0)
    rm.register_fill("tail_end", +10.0)  # reset
    assert rm.state.consecutive_losses.get("tail_end", 0) == 0


def test_adjusted_size_cuts_micro_spread_to_25pct():
    rm = RiskManager()
    # At $1000 capital the standard profile caps per-trade at $20, so feeding
    # $40 in still clamps at 20 before the 25% high-risk cut (-> 5.0).
    base = 40.0
    assert rm.adjusted_size("arbitrage", base) == 20.0
    assert rm.adjusted_size("micro_spread", base) == 5.0  # 20 * 0.25
    assert rm.adjusted_size("dip_arb", base) == 5.0


def test_drawdown_halves_size():
    rm = RiskManager()
    rm.state.current_equity = 890.0  # 11% below the $1000 peak
    assert rm._in_drawdown() is True
    # Base input $100 is clamped to the profile's $20, then halved -> $10.
    assert rm.adjusted_size("arbitrage", 100.0) == 10.0


def test_api_error_streak_global_pauses():
    rm = RiskManager()
    for _ in range(CFG.api_error_pause_threshold):
        rm.register_api_error()
    assert rm.state.global_paused_until > time.time()
    ok, reason = rm.allow(_opp())
    assert not ok
    assert reason == "global_paused"
