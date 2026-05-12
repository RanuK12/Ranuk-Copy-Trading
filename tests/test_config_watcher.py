"""Tests for the config_live.yaml hot-reload."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.core import config_watcher
from bot.monitoring.commands import StrategyController


@pytest.fixture
def fresh_controller(monkeypatch) -> StrategyController:
    import bot.monitoring.commands as cm

    cm._CONTROLLER = StrategyController()
    return cm._CONTROLLER


def _write_yaml(path: Path, content: str) -> None:
    path.write_text(content)


def test_apply_enables_disables_strategies(fresh_controller, tmp_path):
    yaml_src = """
strategies:
  arbitrage:
    enabled: false
  tail_end:
    enabled: true
    sizing: 0.5
""".strip()
    cfg_path = tmp_path / "config_live.yaml"
    _write_yaml(cfg_path, yaml_src)

    parsed = config_watcher._parse(cfg_path)
    assert parsed is not None
    config_watcher._apply(parsed)

    assert fresh_controller.is_enabled("arbitrage") is False
    assert fresh_controller.is_enabled("tail_end") is True
    assert fresh_controller.sizing_multiplier("tail_end") == 0.5


def test_apply_ignores_invalid_sizing_value(fresh_controller, tmp_path):
    yaml_src = """
strategies:
  arbitrage:
    sizing: "not-a-number"
""".strip()
    cfg_path = tmp_path / "config_live.yaml"
    _write_yaml(cfg_path, yaml_src)

    parsed = config_watcher._parse(cfg_path)
    assert parsed is not None
    # Apply should swallow the bad value and leave the default (1.0) intact.
    config_watcher._apply(parsed)
    assert fresh_controller.sizing_multiplier("arbitrage") == 1.0


def test_parse_returns_none_for_missing_file(tmp_path):
    missing = tmp_path / "no-such.yaml"
    assert config_watcher._parse(missing) is None


def test_parse_handles_malformed_yaml(tmp_path):
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("strategies: [::::]\n")
    result = config_watcher._parse(cfg)
    # Malformed YAML -> None + logged warning (no crash).
    assert result is None


def test_config_watcher_start_with_missing_file_is_safe(tmp_path):
    missing = tmp_path / "missing.yaml"
    watcher = config_watcher.ConfigWatcher(path=missing, poll_interval=0.05)
    # Should not raise even if the file doesn't exist.
    watcher.start()
    watcher.stop()
