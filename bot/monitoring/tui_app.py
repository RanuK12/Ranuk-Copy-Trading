"""Textual TUI for the Polymarket trading bot.

Interactive replacement for the v2 Rich Live dashboard. Offers:

* Reactive widgets wired to ``RiskManager`` / ``StateStore`` / ``OpportunityQueue``.
* Toggle strategies ON/OFF with <space> on the Strategies table.
* Equity sparkline over the last 50 fills.
* A command bar that accepts every command from
  :class:`bot.monitoring.commands.CommandProcessor`.
* Keyboard shortcuts:
    q        quit
    tab      focus next panel
    space    toggle currently highlighted strategy
    ctrl+l   clear fill log
    /        jump cursor to command bar

Falls back cleanly if the terminal is too narrow or Textual isn't
installed — the orchestrator picks the Rich Live dashboard in that
case.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional

from bot.config import CFG
from bot.executor import Executor
from bot.logger import get_logger
from bot.monitoring.commands import (
    CommandResult,
    get_controller,
    get_processor,
)
from bot.queue import OpportunityQueue
from bot.risk import get_risk
from bot.scanner import MarketScanner
from bot.state import get_state

log = get_logger("tui")


try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Grid, Vertical
    from textual.reactive import reactive
    from textual.widgets import (
        DataTable,
        Footer,
        Header,
        Input,
        RichLog,
        Sparkline,
        Static,
    )
    _TEXTUAL_AVAILABLE = True
except Exception as e:  # noqa: BLE001
    log.warning(f"Textual not available ({e}). TUI will be unavailable.")
    _TEXTUAL_AVAILABLE = False


_TCSS_PATH = Path(__file__).parent / "dashboard.tcss"


if _TEXTUAL_AVAILABLE:

    class PolymarketTUI(App):
        """Full-screen terminal dashboard and REPL."""

        CSS_PATH = str(_TCSS_PATH)
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("ctrl+c", "quit", "Quit", show=False),
            Binding("tab", "focus_next", "Next panel"),
            Binding("shift+tab", "focus_previous", "Prev panel"),
            Binding("space", "toggle_strategy", "Toggle strat"),
            Binding("ctrl+l", "clear_fills", "Clear fills"),
            Binding("slash", "focus_command", "Command"),
        ]

        # Reactive state that drives the header text
        equity = reactive(0.0)
        pnl_day = reactive(0.0)
        pnl_month = reactive(0.0)
        kill_switch = reactive(False)

        def __init__(
            self,
            queue: OpportunityQueue,
            scanner: MarketScanner,
            executor: Executor,
            *,
            title: str = "Polymarket Bot",
        ) -> None:
            super().__init__()
            self.title = title
            self._queue = queue
            self._scanner = scanner
            self._executor = executor
            self._risk = get_risk()
            self._state = get_state()
            self._ctrl = get_controller()
            self._processor = get_processor()
            self._started_at = time.time()
            self._equity_history: list[float] = []
            self._last_fill_ts = 0.0

        # --------------------------------------------------------------
        # Compose
        # --------------------------------------------------------------
        def compose(self) -> ComposeResult:
            yield Header(show_clock=True, name=self.title)
            with Grid(id="main-grid"):
                with Vertical(id="strategies-panel", classes="panel"):
                    yield Static("📊 Strategies", classes="panel-title")
                    yield DataTable(id="strategies-table", cursor_type="row")
                with Vertical(id="scanner-panel", classes="panel"):
                    yield Static("🔍 Scanner", classes="panel-title")
                    yield DataTable(id="markets-table", cursor_type="row")
                with Vertical(id="queue-panel", classes="panel"):
                    yield Static("⚡ Queue", classes="panel-title")
                    yield DataTable(id="queue-table", cursor_type="row")
                with Vertical(id="fills-panel", classes="panel"):
                    yield Static("📝 Recent Fills", classes="panel-title")
                    yield RichLog(id="fills-log", highlight=True, markup=True)
                with Vertical(id="chart-panel", classes="panel"):
                    yield Static("📈 Equity (last 50 fills)", classes="panel-title")
                    yield Sparkline([], id="equity-spark")
            yield Static(id="command-status")
            yield Input(
                id="command-bar",
                placeholder="Type /help for commands (Enter to send, ↑↓ history)",
            )
            yield Footer()

        async def on_mount(self) -> None:
            self._init_tables()
            self.refresh_all()
            # Periodic refresh — 1Hz is plenty for a trading dashboard.
            self.set_interval(CFG.dashboard_refresh, self.refresh_all)
            # Watch for timed-resume expiry (set by "/resume 30m").
            self.set_interval(5.0, self._check_timed_resume)

        # --------------------------------------------------------------
        # Table initialization
        # --------------------------------------------------------------
        def _init_tables(self) -> None:
            st = self.query_one("#strategies-table", DataTable)
            st.add_columns("Strategy", "On", "Size×", "Trades", "WR", "PnL")

            sc = self.query_one("#markets-table", DataTable)
            sc.add_columns("Slug", "YES", "NO", "Sum", "Vol")

            q = self.query_one("#queue-table", DataTable)
            q.add_columns("P", "Strat", "Market", "Edge", "Conf")

        # --------------------------------------------------------------
        # Refresh cycle
        # --------------------------------------------------------------
        def refresh_all(self) -> None:
            try:
                self._refresh_header()
                self._refresh_strategies()
                self._refresh_scanner()
                self._refresh_queue()
                self._refresh_fills()
                self._refresh_sparkline()
                self._refresh_status()
            except Exception as e:  # noqa: BLE001
                log.debug(f"refresh cycle error: {e}")

        def _refresh_header(self) -> None:
            snap = self._risk.snapshot()
            self.equity = float(snap["equity"])
            self.pnl_day = float(snap["pnl_day"])
            self.pnl_month = float(snap["pnl_month"])
            self.kill_switch = bool(snap["kill_switch"])
            uptime = int(time.time() - self._started_at)
            self.title = (
                f"Polymarket [{CFG.mode.upper()}]  "
                f"equity=${self.equity:.2f}  "
                f"day={self.pnl_day:+.2f}  "
                f"month={self.pnl_month:+.2f}  "
                f"kill={'ON' if self.kill_switch else 'off'}  "
                f"uptime={uptime // 3600}h{(uptime % 3600) // 60:02d}m"
            )
            # Re-set the Header widget's name so it redraws
            for h in self.query(Header):
                h.name = self.title

        def _refresh_strategies(self) -> None:
            t = self.query_one("#strategies-table", DataTable)
            t.clear()
            for name in sorted(CFG.strategies_enabled):
                s = self._state.get_stats(name)
                on = "[green]ON[/]" if self._ctrl.is_enabled(name) else "[red]off[/]"
                size = f"{self._ctrl.sizing_multiplier(name):.2f}"
                wr = f"{s.win_rate * 100:.0f}%" if s.trades else "-"
                pnl_style = "green" if s.pnl_usdc >= 0 else "red"
                t.add_row(
                    name,
                    on,
                    size,
                    str(s.trades),
                    wr,
                    f"[{pnl_style}]{s.pnl_usdc:+.2f}[/]",
                    key=name,
                )

        def _refresh_scanner(self) -> None:
            t = self.query_one("#markets-table", DataTable)
            t.clear()
            snap = self._scanner.snapshot
            # Show the 20 highest-volume enriched markets
            markets = sorted(
                (em for em in snap.markets.values() if em.yes_ask is not None),
                key=lambda em: -em.market.volume_usdc,
            )[:20]
            for em in markets:
                ya = f"{em.yes_ask:.3f}" if em.yes_ask is not None else "-"
                na = f"{em.no_ask:.3f}" if em.no_ask is not None else "-"
                total = em.sum_yes_no
                total_str = f"{total:.3f}" if total is not None else "-"
                # Highlight sub-unity sums (arb candidates)
                if total is not None and total < 0.99:
                    total_str = f"[bold yellow]{total_str}[/]"
                t.add_row(
                    em.market.slug[:32],
                    ya,
                    na,
                    total_str,
                    f"${em.market.volume_usdc:,.0f}",
                )

        def _refresh_queue(self) -> None:
            t = self.query_one("#queue-table", DataTable)
            t.clear()
            for opp in self._queue.snapshot()[:12]:
                t.add_row(
                    str(opp.priority),
                    opp.strategy,
                    opp.market_slug[:32],
                    f"{opp.expected_profit_pct * 100:+.2f}%",
                    f"{opp.confidence:.2f}",
                )

        def _refresh_fills(self) -> None:
            log_widget = self.query_one("#fills-log", RichLog)
            for f in reversed(self._state.recent_fills(limit=15)):
                ts = float(f.get("executed_at") or 0)
                if ts <= self._last_fill_ts:
                    continue
                self._last_fill_ts = ts
                strategy = f.get("strategy", "?")
                if self._ctrl.log_muted(strategy):
                    continue
                status = f.get("status", "?")
                pnl = float(f.get("pnl_usdc") or 0.0)
                style = {
                    "simulated": "cyan",
                    "filled": "green",
                    "skipped": "yellow",
                    "failed": "red",
                }.get(status, "white")
                market = str(f.get("market_id", ""))[:24]
                reason = str(f.get("reason", ""))[:30]
                log_widget.write(
                    f"[{style}]{status:<9}[/] {strategy:<14} {market:<24} "
                    f"pnl={pnl:+.2f}  {reason}"
                )

        def _refresh_sparkline(self) -> None:
            # Build equity curve from recent fills (newest last)
            fills = list(reversed(self._state.recent_fills(limit=50)))
            equity = 0.0
            self._equity_history.clear()
            for f in fills:
                equity += float(f.get("pnl_usdc") or 0.0)
                self._equity_history.append(equity)
            spark = self.query_one("#equity-spark", Sparkline)
            spark.data = self._equity_history or [0]

        def _refresh_status(self) -> None:
            snap = self._scanner.snapshot
            status = self.query_one("#command-status", Static)
            age = int(time.time() - snap.generated_at) if snap.generated_at else -1
            status.update(
                f"scanning: last scan {age}s ago | "
                f"markets={len(snap.markets)} "
                f"arb={len(snap.arbitrage_candidates)} "
                f"tail={len(snap.tail_end_candidates)} "
                f"queue={len(self._queue)} "
                f"inflight={self._executor.inflight}"
            )

        def _check_timed_resume(self) -> None:
            # If /resume NN was used, the orchestrator periodically compares
            # global_paused_until to now — when reached we flip kill back on.
            now = time.time()
            s = self._risk.state
            if s.global_paused_until and now >= s.global_paused_until and not s.kill_switch:
                self._risk.trigger_kill_switch("timed_resume_expired")

        # --------------------------------------------------------------
        # Event handlers
        # --------------------------------------------------------------
        async def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id != "command-bar":
                return
            raw = event.value.strip()
            event.input.value = ""
            if not raw:
                return
            await self._run_command(raw)

        async def _run_command(self, raw: str) -> None:
            result = self._processor.dispatch(raw)
            log_widget = self.query_one("#fills-log", RichLog)
            # Commands can signal actions via result.data["action"]
            action = result.data.get("action")
            if action == "clear_fills":
                log_widget.clear()
                log_widget.write("[grey]fills cleared[/]")
                return
            if action == "shutdown":
                log_widget.write("[yellow]shutdown requested[/]")
                self.exit()
                return
            style = "green" if result.success else "red"
            for line in (result.message or "").splitlines() or [""]:
                log_widget.write(f"[{style}]> {line}[/]")

        # -- Keyboard actions ------------------------------------------
        def action_toggle_strategy(self) -> None:
            t = self.query_one("#strategies-table", DataTable)
            if t.row_count == 0:
                return
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key
            if key is None:
                return
            name = key.value if hasattr(key, "value") else str(key)
            if not name:
                return
            new_state = not self._ctrl.is_enabled(name)
            self._ctrl.set_enabled(name, new_state)
            self._refresh_strategies()

        def action_clear_fills(self) -> None:
            self.query_one("#fills-log", RichLog).clear()

        def action_focus_command(self) -> None:
            self.query_one("#command-bar", Input).focus()

else:  # pragma: no cover — Textual not installed

    class PolymarketTUI:  # type: ignore[no-redef]
        """Stub used when Textual is unavailable."""

        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "Textual is not installed. Run `pip install textual` "
                "or use --dashboard none."
            )


async def run_tui(
    queue: OpportunityQueue,
    scanner: MarketScanner,
    executor: Executor,
) -> None:
    """Async entry point: blocks until the user quits the TUI."""
    if not _TEXTUAL_AVAILABLE:
        raise RuntimeError("Textual not installed.")
    app = PolymarketTUI(queue, scanner, executor)
    await app.run_async()


def is_available() -> bool:
    return _TEXTUAL_AVAILABLE
