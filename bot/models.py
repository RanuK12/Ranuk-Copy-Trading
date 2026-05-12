"""Core dataclasses shared across the bot.

These are intentionally simple (no Pydantic at the hot path) to keep the
scanner/queue/executor pipeline fast and easy to reason about.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderKind(str, Enum):
    FOK = "FOK"  # Fill-or-Kill market order (arbitrage)
    GTC = "GTC"  # Good-Til-Cancelled limit order (sniper / MM)
    LIMIT = "LIMIT"  # Slippage-controlled limit (copy / tail-end)


# ---- Priorities (lower = more urgent) ---------------------------------------
PRIORITY_ARBITRAGE = 0
PRIORITY_TAIL_END = 10
PRIORITY_DIP_ARB = 15
PRIORITY_MICRO_SPREAD = 20
PRIORITY_MARKET_MAKING = 20
PRIORITY_SMART_COPY = 30
PRIORITY_SNIPER = 40


@dataclass
class Leg:
    """A single order leg (one order to send).

    Arbitrage opportunities emit multiple legs that must be sent together.
    """

    token_id: str
    side: Side
    size_usdc: float
    kind: OrderKind = OrderKind.FOK
    limit_price: Optional[float] = None  # Required for GTC/LIMIT


@dataclass
class Opportunity:
    """A trade idea emitted by a strategy and consumed by the executor."""

    strategy: str
    market_id: str  # conditionId
    market_slug: str
    priority: int = PRIORITY_SMART_COPY
    confidence: float = 0.0  # 0..1
    expected_profit_pct: float = 0.0
    legs: list[Leg] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Slippage control (applies to FOK/LIMIT single-leg orders)
    max_slippage: Optional[float] = None
    reference_price: Optional[float] = None

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    # --- Priority-queue support (min-heap on priority, tiebreak by time) ---
    def __lt__(self, other: "Opportunity") -> bool:  # noqa: D401
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.created_at < other.created_at


@dataclass
class Fill:
    """Outcome of executing an :class:`Opportunity`."""

    opportunity_id: str
    strategy: str
    market_id: str
    status: str  # filled | skipped | failed | simulated
    pnl_usdc: float = 0.0
    reason: str = ""
    tx_hashes: list[str] = field(default_factory=list)
    executed_at: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)
