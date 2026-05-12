"""Shared pytest fixtures.

Sets safe defaults for env-driven config so tests never hit real APIs or
the filesystem.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Pre-populate environment BEFORE bot.config is imported anywhere.
os.environ.setdefault("MODE", "paper")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("STRATEGIES_ENABLED", "arbitrage,tail_end")
os.environ.setdefault("TOTAL_CAPITAL_USDC", "1000")
os.environ.setdefault("DEFAULT_TRADE_SIZE_USDC", "20")
os.environ.setdefault("ALCHEMY_HTTP_URL", "https://polygon-rpc.com")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("SUPABASE_URL", "")


@pytest.fixture(autouse=True)
def _tmp_state_file(monkeypatch):
    """Every test gets its own state file under a tmp dir."""
    with tempfile.TemporaryDirectory() as td:
        state = Path(td) / "state.json"
        monkeypatch.setenv("STATE_FILE", str(state))
        # Reload config + reset singletons that might have captured the old path
        import importlib

        import bot.config as cfg_mod

        importlib.reload(cfg_mod)
        import bot.state as state_mod

        state_mod.STATE = None
        import bot.risk as risk_mod

        risk_mod.RISK = None
        yield state
