"""Tests for the PositionMonitor agonizing-position drain and capital sync.

Covers the night-of-audit fixes:
  * a BUY-side position bought at ~5¢+ whose cur_price collapsed under 1¢
    triggers a forced SELL so we recover residual value.
  * dead-as-a-doornail positions (cur_price ~ 0) are not repeatedly hammered.
  * capital sync updates CFG.total_capital_usdc from Polymarket reality.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from bot.config import CFG
from bot.position_monitor import PositionMonitor


@contextmanager
def _cfg(**overrides):
    prev = {k: getattr(CFG, k) for k in overrides}
    try:
        for k, v in overrides.items():
            object.__setattr__(CFG, k, v)
        yield
    finally:
        for k, v in prev.items():
            object.__setattr__(CFG, k, v)


class _FakePoly:
    """Minimal stand-in for PolymarketClient used by the monitor."""

    def __init__(self, positions, portfolio_value=None):
        self._positions = positions
        self._portfolio_value = portfolio_value
        self.sell_calls: list[tuple[str, float]] = []
        self._sell_result = {"success": True, "response": {}}

    def set_sell_result(self, result):
        self._sell_result = result

    def get_user_positions(self, _funder):
        return self._positions

    def get_portfolio_value(self, _funder):
        return self._portfolio_value

    async def sell_position_async(self, token_id, shares):
        self.sell_calls.append((token_id, shares))
        return self._sell_result


def _mk_monitor(fake):
    pm = PositionMonitor()
    pm._poly = fake
    pm._tg = MagicMock()

    async def _noop(*a, **kw):
        return None

    pm._tg.send = _noop
    return pm


@pytest.mark.asyncio
async def test_agonizing_position_triggers_forced_sell():
    """Entry 6¢, current 0.05¢ → forced sell to recover remaining value."""
    with _cfg(mode="live", poly_funder="0xFUNDER", stop_loss_pct=0.25, take_profit_pct=0.40):
        fake = _FakePoly(
            positions=[
                {
                    "avgPrice": 0.06,
                    "curPrice": 0.0005,
                    "size": 100.0,
                    "asset": "tok-1",
                    "title": "Counter-Strike Liquid vs M80",
                }
            ],
        )
        pm = _mk_monitor(fake)
        await pm._check_live_polymarket_positions()
        assert fake.sell_calls == [("tok-1", 100.0)], "should SELL the agonizing size"


@pytest.mark.asyncio
async def test_agonizing_position_cooldown_prevents_spam():
    """Same position within cooldown window → only one sell attempt."""
    with _cfg(mode="live", poly_funder="0xFUNDER"):
        fake = _FakePoly(
            positions=[
                {
                    "avgPrice": 0.10,
                    "curPrice": 0.002,
                    "size": 50.0,
                    "asset": "tok-1",
                    "title": "test",
                }
            ],
        )
        pm = _mk_monitor(fake)
        await pm._check_live_polymarket_positions()
        await pm._check_live_polymarket_positions()
        assert len(fake.sell_calls) == 1, "cooldown should de-duplicate sell attempts"


@pytest.mark.asyncio
async def test_healthy_low_entry_position_is_not_agonizing():
    """Entry at 0.02 (sniper ladder), current 0.018 (-10%, above SL) →
    NOT agonizing AND below the regular stop-loss threshold.

    Confirms the agonizing drain does not fire for low-entry sniper bets
    that are merely sitting idle with a small unrealised loss.
    """
    with _cfg(mode="live", poly_funder="0xFUNDER", stop_loss_pct=0.25, take_profit_pct=0.40):
        fake = _FakePoly(
            positions=[
                {
                    "avgPrice": 0.02,
                    "curPrice": 0.018,
                    "size": 200.0,
                    "asset": "tok-sniper",
                    "title": "sniper bet",
                }
            ],
        )
        pm = _mk_monitor(fake)
        await pm._check_live_polymarket_positions()
        assert fake.sell_calls == [], (
            "sniper bets with small unrealised losses and avg_price < 5¢ must "
            "never trigger the agonizing drain"
        )


@pytest.mark.asyncio
async def test_capital_sync_updates_cfg():
    """When real portfolio = $6.49, CFG.total_capital_usdc should match."""
    with _cfg(mode="live", poly_funder="0xFUNDER", total_capital_usdc=10.0):
        fake = _FakePoly(positions=[], portfolio_value=6.49)
        pm = _mk_monitor(fake)
        await pm._sync_capital_from_polymarket()
        assert CFG.total_capital_usdc == pytest.approx(6.49)


@pytest.mark.asyncio
async def test_capital_sync_skipped_in_paper():
    with _cfg(mode="paper", total_capital_usdc=100.0):
        fake = _FakePoly(positions=[], portfolio_value=6.49)
        pm = _mk_monitor(fake)
        await pm._sync_capital_from_polymarket()
        assert CFG.total_capital_usdc == 100.0, "paper mode must not mutate capital"
