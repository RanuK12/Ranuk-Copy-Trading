"""Strategy base class.

All strategies inherit from :class:`Strategy` and override :meth:`generate`
to emit zero or more :class:`Opportunity` objects per scan cycle.

The orchestrator drives the strategy with the shared ``MarketSnapshot``;
strategies must **not** hit Gamma/CLOB on their own unless they need data
the scanner does not expose (e.g. Data-API user trades for smart-copy).

Design rules
------------
* Strategies are pure: ``generate`` returns opportunities, it does not push
  to the queue. That keeps them trivially testable.
* Sizing is delegated to :func:`bot.risk.RiskManager.adjusted_size` so all
  per-strategy risk cuts live in one place.
* Any extra I/O (Binance, Data-API, etc.) goes inside the strategy module
  and is isolated from other strategies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterable

from bot.config import CFG
from bot.logger import get_logger
from bot.models import Opportunity
from bot.risk import get_risk
from bot.scanner import MarketSnapshot


class Strategy(ABC):
    """Abstract strategy. Subclasses set ``name`` and implement ``generate``."""

    #: Short snake_case identifier that matches :data:`CFG.strategies_enabled`
    #: and the key used in :data:`bot.strategies.REGISTRY`.
    name: str = "base"

    def __init__(self) -> None:
        self.log = get_logger(f"strategy.{self.name}")
        self.risk = get_risk()

    @abstractmethod
    async def generate(self, snap: MarketSnapshot) -> Iterable[Opportunity]:
        """Emit opportunities based on the current :class:`MarketSnapshot`."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def size_usdc(self, base: float | None = None) -> float:
        """Risk-adjusted dollar size for this strategy."""
        return self.risk.adjusted_size(self.name, base or CFG.default_trade_size)
