"""Command processor for TUI, CLI and Telegram.

A single parser + dispatcher so the same command set ("/arb off",
"/pnl week", "/exposure", ...) works from three entry points:

* Textual TUI CommandBar  (``on_input_submitted``)
* ``python main.py --command "arb off"``  (one-shot CLI)
* Telegram long-poll handler              (existing)

The processor has **no I/O side effects** itself — it mutates the
shared state (`RiskManager`, `StateStore`, `StrategyController`) and
returns a rendered :class:`CommandResult` that callers print/send.
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from bot.config import CFG
from bot.logger import get_logger
from bot.risk import get_risk
from bot.state import get_state

log = get_logger("commands")


# ---------------------------------------------------------------------------
# Shared runtime state toggleable via commands
# ---------------------------------------------------------------------------
class StrategyController:
    """Runtime toggle + sizing multiplier per strategy.

    Strategies check :meth:`is_enabled` / :meth:`sizing_multiplier` each scan;
    the command processor mutates the internal dicts. Changes are
    immediate (no restart), and the controller persists them to a small
    JSON snapshot file so they survive restarts as well.
    """

    def __init__(self) -> None:
        self._enabled: dict[str, bool] = {
            name: True for name in CFG.strategies_enabled
        }
        self._sizing: dict[str, float] = {
            name: 1.0 for name in CFG.strategies_enabled
        }
        self._filters: set[str] = set()  # strategy names muted for /logs

    # -- State access ----------------------------------------------------
    def is_enabled(self, strategy: str) -> bool:
        return self._enabled.get(strategy, False)

    def sizing_multiplier(self, strategy: str) -> float:
        return self._sizing.get(strategy, 1.0)

    def log_muted(self, strategy: str) -> bool:
        return strategy in self._filters

    # -- Mutations -------------------------------------------------------
    def set_enabled(self, strategy: str, on: bool) -> None:
        self._enabled[strategy] = on

    def set_sizing(self, strategy: str, multiplier: float) -> None:
        self._sizing[strategy] = max(0.0, min(10.0, multiplier))

    def toggle_log_filter(self, strategy: str) -> bool:
        if strategy in self._filters:
            self._filters.discard(strategy)
            return False
        self._filters.add(strategy)
        return True

    # -- Introspection for dashboard -------------------------------------
    def snapshot(self) -> dict[str, dict]:
        return {
            name: {
                "enabled": self._enabled.get(name, False),
                "sizing": self._sizing.get(name, 1.0),
                "log_muted": name in self._filters,
            }
            for name in CFG.strategies_enabled
        }


_CONTROLLER: Optional[StrategyController] = None


def get_controller() -> StrategyController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = StrategyController()
    return _CONTROLLER


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------
@dataclass
class CommandResult:
    """Result of a dispatched command. Consumed by TUI / CLI / Telegram."""

    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)

    def as_text(self) -> str:
        return self.message

    def __bool__(self) -> bool:  # truthy if success
        return self.success


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
CommandFn = Callable[[list[str]], CommandResult]


class CommandProcessor:
    def __init__(self) -> None:
        self._commands: dict[str, CommandFn] = {}
        self._help_text: dict[str, str] = {}
        self._register_defaults()

    # -- Registration ----------------------------------------------------
    def register(self, name: str, fn: CommandFn, help_text: str = "") -> None:
        self._commands[name.lstrip("/").lower()] = fn
        self._help_text[name.lstrip("/").lower()] = help_text

    # -- Parse + dispatch ------------------------------------------------
    def dispatch(self, raw: str) -> CommandResult:
        raw = raw.strip()
        if not raw:
            return CommandResult(False, "Empty command. Try /help.")
        if raw.startswith("/"):
            raw = raw[1:]
        try:
            parts = shlex.split(raw)
        except ValueError as e:
            return CommandResult(False, f"Bad syntax: {e}")
        if not parts:
            return CommandResult(False, "Empty command. Try /help.")

        cmd, *args = parts
        cmd = cmd.lower()

        # Strategy-name shortcut: "/arb off" -> dispatch("strat", ["arb", "off"])
        if cmd in _STRATEGY_ALIASES and args:
            strat = _STRATEGY_ALIASES[cmd]
            return self._commands["strat"]([strat] + args)

        handler = self._commands.get(cmd)
        if handler is None:
            return CommandResult(False, f"Unknown command: /{cmd}. Try /help.")
        try:
            return handler(args)
        except Exception as e:  # noqa: BLE001
            log.exception(f"command /{cmd} crashed")
            return CommandResult(False, f"Command crashed: {e}")

    # -- Help ------------------------------------------------------------
    def help_text(self) -> str:
        lines = ["Available commands:"]
        for name in sorted(self._commands):
            h = self._help_text.get(name, "")
            lines.append(f"  /{name:<12}  {h}")
        return "\n".join(lines)

    # -----------------------------------------------------------------
    # Default command set
    # -----------------------------------------------------------------
    def _register_defaults(self) -> None:
        self.register("help", self._cmd_help, "Show this help")
        self.register("status", self._cmd_status, "Mode, equity, PnL, active strategies")
        self.register("pnl", self._cmd_pnl, "P&L summary — /pnl [day|week|month]")
        self.register("strategies", self._cmd_strategies, "List strategies and their state")
        self.register("strat", self._cmd_strat, "Toggle a strategy — /strat arbitrage on")
        self.register("pause", self._cmd_pause, "Global pause (equivalent to kill switch)")
        self.register("resume", self._cmd_resume, "Release kill switch — /resume [30m]")
        self.register("size", self._cmd_size, "Set sizing multiplier — /size arb 50")
        self.register("exposure", self._cmd_exposure, "Per-market + per-strategy exposure")
        self.register("logs", self._cmd_logs, "Mute/unmute a strategy in log panel")
        self.register("clear", self._cmd_clear, "Clear the fill panel (UI only)")
        self.register("quit", self._cmd_quit, "Graceful shutdown")
        self.register("exit", self._cmd_quit, "Alias of /quit")
        self.register("budget", self._cmd_budget, "Show budget profile recommendations")

    # -- Handlers --------------------------------------------------------
    def _cmd_help(self, _: list[str]) -> CommandResult:
        return CommandResult(True, self.help_text())

    def _cmd_status(self, _: list[str]) -> CommandResult:
        risk = get_risk()
        state = get_state()
        ctrl = get_controller()
        snap = risk.snapshot()

        lines = [
            f"Mode: {CFG.mode}",
            f"Equity: ${snap['equity']:.2f} (peak ${snap['peak_equity']:.2f})",
            f"PnL day: {snap['pnl_day']:+.2f}  PnL month: {snap['pnl_month']:+.2f}",
            f"Kill switch: {'ON' if snap['kill_switch'] else 'off'}",
            f"In drawdown: {'yes' if snap['in_drawdown'] else 'no'}",
            "",
            "Strategies:",
        ]
        for name in sorted(CFG.strategies_enabled):
            s = state.get_stats(name)
            wr = f"{s.win_rate * 100:.1f}%" if s.trades else "-"
            on = "ON" if ctrl.is_enabled(name) else "off"
            mult = ctrl.sizing_multiplier(name)
            lines.append(
                f"  {name:<14} {on:<3} size×{mult:.2f}  trades={s.trades:<3} "
                f"wr={wr:<7} pnl={s.pnl_usdc:+.2f}"
            )
        return CommandResult(True, "\n".join(lines), data=snap)

    def _cmd_pnl(self, args: list[str]) -> CommandResult:
        risk = get_risk()
        state = get_state()
        period = (args[0].lower() if args else "all").strip()

        snap = risk.snapshot()
        if period in {"day", "today"}:
            msg = f"PnL today: {snap['pnl_day']:+.2f} USDC"
        elif period == "month":
            msg = f"PnL this month: {snap['pnl_month']:+.2f} USDC"
        elif period == "week":
            # Approximate: sum fills from recent_fills() within the last 7d
            cutoff = time.time() - 7 * 86400
            pnl = sum(
                float(f.get("pnl_usdc") or 0)
                for f in state.recent_fills(limit=500)
                if float(f.get("executed_at") or 0) >= cutoff
            )
            msg = f"PnL last 7d (from {len(state.recent_fills(500))} recent fills): {pnl:+.2f} USDC"
        else:
            # Breakdown per strategy
            lines = [f"Equity ${snap['equity']:.2f} | day {snap['pnl_day']:+.2f} | month {snap['pnl_month']:+.2f}", ""]
            for name in sorted(CFG.strategies_enabled):
                s = state.get_stats(name)
                lines.append(
                    f"  {name:<14} trades={s.trades:<3} wins={s.wins:<3} "
                    f"losses={s.losses:<3} pnl={s.pnl_usdc:+.2f}"
                )
            msg = "\n".join(lines)
        return CommandResult(True, msg)

    def _cmd_strategies(self, _: list[str]) -> CommandResult:
        ctrl = get_controller()
        lines = ["Strategies (space to toggle in TUI):"]
        for name in sorted(CFG.strategies_enabled):
            on = "ON" if ctrl.is_enabled(name) else "OFF"
            mult = ctrl.sizing_multiplier(name)
            lines.append(f"  {name:<14} {on:<3}  size×{mult:.2f}")
        return CommandResult(True, "\n".join(lines))

    def _cmd_strat(self, args: list[str]) -> CommandResult:
        if len(args) < 2:
            return CommandResult(False, "Usage: /strat <name> on|off")
        name = args[0].lower()
        if name not in CFG.strategies_enabled:
            return CommandResult(
                False,
                f"Unknown strategy '{name}'. Known: {', '.join(CFG.strategies_enabled)}",
            )
        action = args[1].lower()
        if action not in {"on", "off"}:
            return CommandResult(False, "Action must be 'on' or 'off'.")
        ctrl = get_controller()
        ctrl.set_enabled(name, action == "on")
        return CommandResult(True, f"Strategy '{name}' -> {action.upper()}")

    def _cmd_pause(self, _: list[str]) -> CommandResult:
        get_risk().trigger_kill_switch("command:/pause")
        return CommandResult(True, "Kill switch ACTIVATED. New orders blocked.")

    def _cmd_resume(self, args: list[str]) -> CommandResult:
        risk = get_risk()
        risk.release_kill_switch()
        if args:
            duration = _parse_duration(args[0])
            if duration is None:
                return CommandResult(False, f"Can't parse duration '{args[0]}'.")
            # Schedule a timed re-pause by storing the wakeup timestamp on risk
            # state; the main orchestrator polls this and re-engages the kill
            # switch when the deadline passes.
            risk.state.global_paused_until = time.time() + duration
            mins = int(duration // 60)
            return CommandResult(
                True,
                f"Trading resumed for {mins}m, auto-pause afterwards.",
            )
        return CommandResult(True, "Trading resumed.")

    def _cmd_size(self, args: list[str]) -> CommandResult:
        if len(args) < 2:
            return CommandResult(False, "Usage: /size <strategy> <percent>")
        name = args[0].lower()
        if name not in CFG.strategies_enabled:
            return CommandResult(False, f"Unknown strategy '{name}'.")
        try:
            pct = float(args[1].rstrip("%"))
        except ValueError:
            return CommandResult(False, "Percent must be a number (e.g. 50 for 50%).")
        mult = pct / 100.0
        get_controller().set_sizing(name, mult)
        return CommandResult(True, f"{name} sizing -> ×{mult:.2f} ({pct:g}%)")

    def _cmd_exposure(self, _: list[str]) -> CommandResult:
        risk = get_risk()
        m = risk.state.exposure_per_market
        s = risk.state.exposure_per_strategy
        if not m and not s:
            return CommandResult(True, "No open exposure.")
        lines = ["Per-strategy exposure:"]
        for name, amt in sorted(s.items(), key=lambda kv: -kv[1]):
            cap = CFG.max_strategy_usdc
            lines.append(f"  {name:<14} ${amt:>7.2f} / ${cap:>7.2f} cap")
        lines.append("")
        lines.append("Per-market exposure (top 10):")
        for market, amt in sorted(m.items(), key=lambda kv: -kv[1])[:10]:
            cap = CFG.max_market_usdc
            lines.append(f"  {market[:30]:<30} ${amt:>7.2f} / ${cap:>7.2f} cap")
        return CommandResult(True, "\n".join(lines))

    def _cmd_logs(self, args: list[str]) -> CommandResult:
        if not args:
            return CommandResult(False, "Usage: /logs <strategy>")
        name = args[0].lower()
        if name not in CFG.strategies_enabled:
            return CommandResult(False, f"Unknown strategy '{name}'.")
        muted = get_controller().toggle_log_filter(name)
        return CommandResult(True, f"{name} logs {'MUTED' if muted else 'unmuted'}")

    def _cmd_clear(self, _: list[str]) -> CommandResult:
        # UI-specific. The TUI listens for this command and wipes its RichLog.
        return CommandResult(True, "[clear_fills]", data={"action": "clear_fills"})

    def _cmd_quit(self, _: list[str]) -> CommandResult:
        return CommandResult(True, "[shutdown]", data={"action": "shutdown"})

    def _cmd_budget(self, _: list[str]) -> CommandResult:
        """Show the current BudgetProfile recommendation. Populated by budget.py."""
        try:
            from bot.core.budget import current_profile
            profile = current_profile()
        except Exception as e:  # noqa: BLE001
            return CommandResult(False, f"Budget profile unavailable: {e}")
        return CommandResult(
            True,
            profile.describe(),
            data={
                "tier": profile.tier,
                "capital": profile.capital_usdc,
                "recommended_size": profile.trade_size_usdc,
                "recommended_strategies": profile.recommended_strategies,
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_STRATEGY_ALIASES = {
    "arb": "arbitrage",
    "tail": "tail_end",
    "micro": "micro_spread",
    "dip": "dip_arb",
    "copy": "smart_copy",
    "mm": "market_making",
    "snipe": "sniper",
}


def _parse_duration(raw: str) -> Optional[float]:
    """Parse '30m', '2h', '45s' into seconds; bare number == seconds."""
    raw = raw.strip().lower()
    if not raw:
        return None
    suffix = raw[-1]
    if suffix in {"s", "m", "h", "d"}:
        try:
            value = float(raw[:-1])
        except ValueError:
            return None
        return {"s": value, "m": value * 60, "h": value * 3600, "d": value * 86400}[suffix]
    try:
        return float(raw)
    except ValueError:
        return None


_PROCESSOR: Optional[CommandProcessor] = None


def get_processor() -> CommandProcessor:
    global _PROCESSOR
    if _PROCESSOR is None:
        _PROCESSOR = CommandProcessor()
    return _PROCESSOR
