"""Runtime state: positions, fills, and win-rate tracking per strategy.

Two persistence layers:

* **Local JSON** (``CFG.state_file``) — always on, restart-safe.
* **Supabase** (optional) — enabled when SUPABASE_URL / SUPABASE_KEY are set.

The local layer is authoritative for logic decisions; Supabase is a mirror
for dashboards / audits, so transient Supabase outages do not break trading.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from bot.config import CFG
from bot.logger import get_logger
from bot.models import Fill

log = get_logger("state")


@dataclass
class StrategyStats:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usdc: float = 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades) if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        # trivial approximation; detailed PF available in backtest engine
        if self.losses == 0:
            return float("inf") if self.wins > 0 else 0.0
        return self.wins / self.losses


@dataclass
class BotState:
    # Markets already opened by the bot (dedup), per strategy
    positions_by_strategy: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # Latest seen tx hash per watched smart-money wallet
    last_seen_tx: dict[str, str] = field(default_factory=dict)
    # Rolling stats per strategy
    stats: dict[str, StrategyStats] = field(default_factory=dict)
    # Paper ledger (used in paper mode only)
    paper_fills: list[dict[str, Any]] = field(default_factory=list)
    # Recent fills for dashboard (bounded)
    recent_fills: list[dict[str, Any]] = field(default_factory=list)


class StateStore:
    """Thread-safe, on-disk state store plus optional Supabase mirror."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or CFG.state_file
        self._lock = threading.Lock()
        self.state = self._load()
        self._recent: Deque[dict[str, Any]] = deque(self.state.recent_fills, maxlen=50)
        self._supabase = None
        if CFG.supabase_url and CFG.supabase_key:
            try:
                from bot.clients.supabase_client import SupabaseMirror
                self._supabase = SupabaseMirror(CFG.supabase_url, CFG.supabase_key)
            except Exception as e:  # noqa: BLE001
                log.warning(f"Supabase mirror unavailable: {e}")

    # ---- Load/save -------------------------------------------------------
    def _load(self) -> BotState:
        if not self.path.exists():
            return BotState()
        try:
            raw = json.loads(self.path.read_text())
            stats = {
                k: StrategyStats(**v) for k, v in (raw.get("stats") or {}).items()
            }
            return BotState(
                positions_by_strategy=defaultdict(
                    list, raw.get("positions_by_strategy", {})
                ),
                last_seen_tx=raw.get("last_seen_tx", {}),
                stats=defaultdict(StrategyStats, stats),
                paper_fills=raw.get("paper_fills", []),
                recent_fills=raw.get("recent_fills", []),
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"State file unreadable ({e}); starting fresh.")
            return BotState()

    def save(self) -> None:
        with self._lock:
            payload = {
                "positions_by_strategy": dict(self.state.positions_by_strategy),
                "last_seen_tx": self.state.last_seen_tx,
                "stats": {k: asdict(v) for k, v in self.state.stats.items()},
                "paper_fills": self.state.paper_fills,
                "recent_fills": list(self._recent),
            }
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str))
            tmp.replace(self.path)

    # ---- Positions / dedup ----------------------------------------------
    def has_open_position(self, strategy: str, market_id: str) -> bool:
        with self._lock:
            for p in self.state.positions_by_strategy.get(strategy, []):
                if p.get("market_id") == market_id and p.get("open"):
                    return True
            return False

    def open_position(
        self,
        strategy: str,
        market_id: str,
        size_usdc: float,
        extra: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self.state.positions_by_strategy.setdefault(strategy, []).append(
                {
                    "market_id": market_id,
                    "size_usdc": size_usdc,
                    "open": True,
                    "opened_at": time.time(),
                    **(extra or {}),
                }
            )
        self.save()

    def close_position(
        self, strategy: str, market_id: str, pnl_usdc: float
    ) -> None:
        with self._lock:
            for p in self.state.positions_by_strategy.get(strategy, []):
                if p.get("market_id") == market_id and p.get("open"):
                    p["open"] = False
                    p["closed_at"] = time.time()
                    p["pnl_usdc"] = pnl_usdc
                    break
        self.save()

    # ---- Stats ----------------------------------------------------------
    def register_fill(self, fill: Fill) -> None:
        with self._lock:
            stats = self.state.stats.setdefault(fill.strategy, StrategyStats())
            if fill.status in ("filled", "simulated"):
                stats.trades += 1
                stats.pnl_usdc += fill.pnl_usdc
                if fill.pnl_usdc >= 0:
                    stats.wins += 1
                else:
                    stats.losses += 1
            self._recent.appendleft(asdict(fill))
            self.state.recent_fills = list(self._recent)
            if CFG.is_paper:
                self.state.paper_fills.append(asdict(fill))

        # Mirror to Supabase best-effort
        if self._supabase is not None:
            try:
                self._supabase.insert_fill(fill)
            except Exception as e:  # noqa: BLE001
                log.debug(f"Supabase mirror failed: {e}")
        self.save()

    def get_stats(self, strategy: str) -> StrategyStats:
        return self.state.stats.get(strategy, StrategyStats())

    def recent_fills(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(self._recent)[:limit]

    # ---- Smart-copy helpers --------------------------------------------
    def get_last_seen_tx(self, wallet: str) -> Optional[str]:
        return self.state.last_seen_tx.get(wallet)

    def set_last_seen_tx(self, wallet: str, tx_hash: str) -> None:
        with self._lock:
            self.state.last_seen_tx[wallet] = tx_hash


# Global singleton
STATE: Optional[StateStore] = None


def get_state() -> StateStore:
    global STATE
    if STATE is None:
        STATE = StateStore()
    return STATE
