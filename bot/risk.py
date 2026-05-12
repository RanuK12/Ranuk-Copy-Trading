"""Risk manager + circuit breakers.

All sizing / exposure / drawdown checks funnel through :class:`RiskManager`.
Strategies SHOULD NOT bypass this — ``main.py`` wires every opportunity
through ``allow()`` before execution.

Circuit breakers implemented
----------------------------
* Max exposure per market    (``max_exposure_per_market``)
* Max exposure per strategy  (``max_exposure_per_strategy``)
* Daily loss cap             (``daily_loss_cap``)
* Monthly loss cap           (``monthly_loss_cap``)
* Max drawdown from peak     (``max_drawdown``)  -> sizing reduced 50%
* N consecutive losses       (``max_consecutive_losses``) -> pause strat 1h
* API error streak           (``api_error_pause_threshold``) -> global pause
* Remote kill-switch         (``trigger_kill_switch``)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from bot.config import CFG
from bot.logger import get_logger
from bot.models import Opportunity

log = get_logger("risk")

_CONSECUTIVE_PAUSE_SECONDS = 3600  # 1 hour


@dataclass
class RiskState:
    # PnL tracking
    realized_pnl_day: float = 0.0
    realized_pnl_month: float = 0.0
    peak_equity: float = 0.0  # high-water mark (initialized from capital)
    current_equity: float = 0.0

    # Exposure: USDC at risk per dimension
    exposure_per_market: dict[str, float] = field(default_factory=dict)
    exposure_per_strategy: dict[str, float] = field(default_factory=dict)

    # Streaks
    consecutive_losses: dict[str, int] = field(default_factory=dict)
    strategy_paused_until: dict[str, float] = field(default_factory=dict)

    # Global pauses
    api_error_streak: int = 0
    global_paused_until: float = 0.0
    kill_switch: bool = False

    # Rollover markers
    day_bucket: str = ""
    month_bucket: str = ""


def _today_bucket() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _month_bucket() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class RiskManager:
    """Enforces every risk limit defined in the config."""

    def __init__(self) -> None:
        self.state = RiskState(
            peak_equity=CFG.total_capital_usdc,
            current_equity=CFG.total_capital_usdc,
            day_bucket=_today_bucket(),
            month_bucket=_month_bucket(),
        )

    # ------------------------------------------------------------------
    # Kill switch (Telegram /emergencystop)
    # ------------------------------------------------------------------
    def trigger_kill_switch(self, reason: str = "manual") -> None:
        self.state.kill_switch = True
        log.critical(f"[red]KILL SWITCH ACTIVATED[/] reason={reason}")

    def release_kill_switch(self) -> None:
        self.state.kill_switch = False
        log.warning("[yellow]Kill switch released.[/]")

    # ------------------------------------------------------------------
    # API error tracking (called by clients on success/failure)
    # ------------------------------------------------------------------
    def register_api_error(self) -> None:
        self.state.api_error_streak += 1
        if self.state.api_error_streak >= CFG.api_error_pause_threshold:
            pause_until = time.time() + CFG.api_error_pause_seconds
            self.state.global_paused_until = max(self.state.global_paused_until, pause_until)
            log.warning(
                f"[yellow]Too many API errors ({self.state.api_error_streak}); "
                f"global pause for {CFG.api_error_pause_seconds}s.[/]"
            )

    def register_api_success(self) -> None:
        self.state.api_error_streak = 0

    # ------------------------------------------------------------------
    # PnL / equity updates (called by executor after fills)
    # ------------------------------------------------------------------
    def register_fill(self, strategy: str, pnl_usdc: float) -> None:
        self._rollover_buckets()
        self.state.realized_pnl_day += pnl_usdc
        self.state.realized_pnl_month += pnl_usdc
        self.state.current_equity += pnl_usdc
        self.state.peak_equity = max(self.state.peak_equity, self.state.current_equity)

        if pnl_usdc < 0:
            self.state.consecutive_losses[strategy] = (
                self.state.consecutive_losses.get(strategy, 0) + 1
            )
            if self.state.consecutive_losses[strategy] >= CFG.max_consecutive_losses:
                until = time.time() + _CONSECUTIVE_PAUSE_SECONDS
                self.state.strategy_paused_until[strategy] = until
                log.warning(
                    f"[yellow]Strategy '{strategy}' paused 1h "
                    f"(consecutive losses={self.state.consecutive_losses[strategy]}).[/]"
                )
        else:
            self.state.consecutive_losses[strategy] = 0

    def _rollover_buckets(self) -> None:
        today = _today_bucket()
        month = _month_bucket()
        if today != self.state.day_bucket:
            log.info(f"Daily PnL bucket rollover ({self.state.day_bucket} -> {today}).")
            self.state.day_bucket = today
            self.state.realized_pnl_day = 0.0
        if month != self.state.month_bucket:
            log.info(f"Monthly PnL bucket rollover ({self.state.month_bucket} -> {month}).")
            self.state.month_bucket = month
            self.state.realized_pnl_month = 0.0

    # ------------------------------------------------------------------
    # Exposure tracking
    # ------------------------------------------------------------------
    def reserve_exposure(self, strategy: str, market_id: str, size_usdc: float) -> None:
        self.state.exposure_per_market[market_id] = (
            self.state.exposure_per_market.get(market_id, 0.0) + size_usdc
        )
        self.state.exposure_per_strategy[strategy] = (
            self.state.exposure_per_strategy.get(strategy, 0.0) + size_usdc
        )

    def release_exposure(self, strategy: str, market_id: str, size_usdc: float) -> None:
        self.state.exposure_per_market[market_id] = max(
            0.0, self.state.exposure_per_market.get(market_id, 0.0) - size_usdc
        )
        self.state.exposure_per_strategy[strategy] = max(
            0.0, self.state.exposure_per_strategy.get(strategy, 0.0) - size_usdc
        )

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def adjusted_size(self, strategy: str, base_size: float) -> float:
        """Apply budget profile, hot-reload multiplier, high-risk cut and drawdown."""
        # 1. Budget profile owns the base trade size for micro / small tiers.
        try:
            from bot.core.budget import current_profile
            profile = current_profile()
            base_size = min(base_size, profile.trade_size_usdc)
        except Exception:  # noqa: BLE001 — budget module optional
            pass

        size = base_size

        # 2. Command bar / TUI multiplier (e.g. `/size arb 50`).
        try:
            from bot.monitoring.commands import get_controller
            size *= get_controller().sizing_multiplier(strategy)
        except Exception:  # noqa: BLE001
            pass

        # 3. High-risk strategies get 25% of base (micro_spread, dip_arb)
        if strategy in {"micro_spread", "dip_arb"}:
            size *= 0.25

        # 4. Drawdown >= MAX_DRAWDOWN -> 50% sizing
        if self._in_drawdown():
            size *= 0.50

        return round(max(0.0, size), 4)

    def _in_drawdown(self) -> bool:
        if self.state.peak_equity <= 0:
            return False
        drawdown = (self.state.peak_equity - self.state.current_equity) / self.state.peak_equity
        return drawdown >= CFG.max_drawdown

    # ------------------------------------------------------------------
    # Gatekeeping — called for every Opportunity before execution
    # ------------------------------------------------------------------
    def allow(self, opp: Opportunity) -> tuple[bool, str]:
        self._rollover_buckets()
        now = time.time()

        if self.state.kill_switch:
            return False, "kill_switch"
        if now < self.state.global_paused_until:
            return False, "global_paused"
        paused_until = self.state.strategy_paused_until.get(opp.strategy, 0.0)
        if now < paused_until:
            return False, f"strategy_paused:{opp.strategy}"

        # Budget-profile-aware caps (fall back to CFG for standard/large).
        try:
            from bot.core.budget import current_profile
            profile = current_profile()
            daily_cap = profile.daily_loss_cap_usdc
            max_market = profile.max_per_market_usdc
            max_strategy = profile.max_per_strategy_usdc
            monthly_cap = CFG.monthly_loss_usdc
        except Exception:  # noqa: BLE001
            daily_cap = CFG.daily_loss_usdc
            max_market = CFG.max_market_usdc
            max_strategy = CFG.max_strategy_usdc
            monthly_cap = CFG.monthly_loss_usdc

        if -self.state.realized_pnl_day >= daily_cap:
            self.trigger_kill_switch("daily_loss_cap_reached")
            return False, "daily_loss_cap"
        if -self.state.realized_pnl_month >= monthly_cap:
            self.trigger_kill_switch("monthly_loss_cap_reached")
            return False, "monthly_loss_cap"

        # Strategy on/off from the StrategyController (TUI/Telegram hot toggle)
        try:
            from bot.monitoring.commands import get_controller
            if not get_controller().is_enabled(opp.strategy):
                return False, f"strategy_disabled:{opp.strategy}"
        except Exception:  # noqa: BLE001
            pass

        # Exposure caps
        order_size = sum(leg.size_usdc for leg in opp.legs)
        mkt_exp = self.state.exposure_per_market.get(opp.market_id, 0.0)
        if mkt_exp + order_size > max_market + 1e-6:
            return False, "market_exposure_cap"

        strat_exp = self.state.exposure_per_strategy.get(opp.strategy, 0.0)
        if strat_exp + order_size > max_strategy + 1e-6:
            return False, "strategy_exposure_cap"

        return True, "ok"

    # ------------------------------------------------------------------
    # Snapshot for dashboard
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, object]:
        return {
            "equity": self.state.current_equity,
            "peak_equity": self.state.peak_equity,
            "pnl_day": self.state.realized_pnl_day,
            "pnl_month": self.state.realized_pnl_month,
            "in_drawdown": self._in_drawdown(),
            "kill_switch": self.state.kill_switch,
            "global_paused": time.time() < self.state.global_paused_until,
            "strategies_paused": {
                s: max(0.0, until - time.time())
                for s, until in self.state.strategy_paused_until.items()
                if until > time.time()
            },
            "api_error_streak": self.state.api_error_streak,
        }


# Global singleton (imported by main / executor)
RISK: Optional[RiskManager] = None


def get_risk() -> RiskManager:
    global RISK
    if RISK is None:
        RISK = RiskManager()
    return RISK
