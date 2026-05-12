"""One-shot CLI entry for the command processor.

Usage (from main.py):
    python main.py --command "status"
    python main.py --command "pnl week"
    python main.py --command "arb off"

Prints the rendered result to stdout and exits. No TUI, no async loop,
no scanner thread — just loads state, runs the command, writes the
result.
"""

from __future__ import annotations

import sys

from bot.monitoring.commands import get_processor
from bot.state import get_state  # ensure singletons are wired
from bot.risk import get_risk


def run(command: str) -> int:
    # Touch the singletons so the commands that read state/risk work.
    get_state()
    get_risk()

    result = get_processor().dispatch(command)
    sys.stdout.write(result.as_text() + "\n")
    sys.stdout.flush()
    return 0 if result.success else 1
