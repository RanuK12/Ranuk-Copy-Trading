"""Tests for the CommandProcessor used by TUI / CLI / Telegram."""

from __future__ import annotations

import pytest

from bot.config import CFG
from bot.monitoring.commands import (
    CommandProcessor,
    StrategyController,
    _parse_duration,
    get_controller,
)
from bot.risk import get_risk


@pytest.fixture
def processor() -> CommandProcessor:
    # Reset the singleton controller between tests so state doesn't leak.
    import bot.monitoring.commands as cm

    cm._CONTROLLER = StrategyController()
    return CommandProcessor()


def test_unknown_command_is_reported(processor):
    result = processor.dispatch("does-not-exist")
    assert not result.success
    assert "Unknown command" in result.message


def test_help_lists_registered_commands(processor):
    result = processor.dispatch("/help")
    assert result.success
    assert "/status" in result.message
    assert "/pnl" in result.message
    assert "/exposure" in result.message


def test_strategy_alias_shortcut(processor):
    # "/arb off" should route to /strat arbitrage off
    result = processor.dispatch("/arb off")
    assert result.success, result.message
    assert not get_controller().is_enabled("arbitrage")


def test_strat_command_toggles_enable_flag(processor):
    result = processor.dispatch("/strat tail_end off")
    assert result.success
    assert not get_controller().is_enabled("tail_end")

    result = processor.dispatch("/strat tail_end on")
    assert result.success
    assert get_controller().is_enabled("tail_end")


def test_size_command_updates_multiplier(processor):
    result = processor.dispatch("/size arbitrage 50")
    assert result.success
    assert get_controller().sizing_multiplier("arbitrage") == 0.5


def test_size_rejects_unknown_strategy(processor):
    result = processor.dispatch("/size nonexistent 50")
    assert not result.success


def test_size_rejects_non_numeric(processor):
    result = processor.dispatch("/size arbitrage abc")
    assert not result.success


def test_pause_triggers_kill_switch(processor):
    risk = get_risk()
    risk.release_kill_switch()  # start clean
    result = processor.dispatch("/pause")
    assert result.success
    assert risk.state.kill_switch is True


def test_resume_releases_kill_switch(processor):
    risk = get_risk()
    risk.trigger_kill_switch("test")
    assert risk.state.kill_switch
    result = processor.dispatch("/resume")
    assert result.success
    assert risk.state.kill_switch is False


def test_resume_with_duration_schedules_repause(processor):
    risk = get_risk()
    risk.state.global_paused_until = 0
    risk.trigger_kill_switch("test")
    result = processor.dispatch("/resume 30m")
    assert result.success
    assert risk.state.global_paused_until > 0  # armed for re-pause


def test_quit_signals_shutdown_action(processor):
    result = processor.dispatch("/quit")
    assert result.success
    assert result.data.get("action") == "shutdown"


def test_clear_signals_clear_action(processor):
    result = processor.dispatch("/clear")
    assert result.success
    assert result.data.get("action") == "clear_fills"


def test_status_includes_strategies_and_equity(processor):
    result = processor.dispatch("/status")
    assert result.success
    assert "Mode:" in result.message
    assert "Equity:" in result.message


def test_exposure_without_positions_says_no_exposure(processor):
    # Fresh risk manager has no exposure reserved
    import bot.risk as risk_mod

    risk_mod.RISK = None  # force re-instantiation
    result = processor.dispatch("/exposure")
    assert result.success
    assert "No open exposure" in result.message


# Helpers ---------------------------------------------------------------
def test_parse_duration_units():
    assert _parse_duration("30s") == 30
    assert _parse_duration("15m") == 15 * 60
    assert _parse_duration("2h") == 2 * 3600
    assert _parse_duration("1d") == 86400
    assert _parse_duration("5.5") == 5.5
    assert _parse_duration("nope") is None
