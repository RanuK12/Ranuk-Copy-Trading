"""Centralized Rich-powered logging.

Importing :func:`get_logger` anywhere yields a logger that shares the same
RichHandler, so all modules write to a single pretty console stream.
"""

from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

from bot.config import CFG

# Shared Rich Console — the dashboard uses the same instance, so log lines
# and live tables never overlap.
CONSOLE = Console()

_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=CFG.log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=CONSOLE,
                rich_tracebacks=True,
                show_path=False,
                markup=True,
            )
        ],
    )
    # Silence noisy third-party loggers
    for noisy in ("urllib3", "httpx", "httpcore", "websockets", "web3.providers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)
