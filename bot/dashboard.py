"""Rich live dashboard for the multi-strategy bot.

A single :class:`~rich.live.Live` view refreshed once per second renders
five panels:

* Header       — mode, capital, PnL (day / month), kill-switch status
* Strategies   — per-strategy trades / wins / losses / win-rate / PnL
* Queue        — top pending opportunities (priority order)
* Recent Fills — last N executions
* Connectivity — RPC / CLOB / Telegram health + last scan duration

The dashboard takes no side effects; it only reads the shared singletons.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from rich.align import Align
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.clients.rpc import get_rpc
from bot.clients.telegram import get_telegram
from bot.config import CFG
from bot.executor import Executor
from bot.logger import CONSOLE, get_logger
from bot.queue import OpportunityQueue
from bot.risk import get_risk
from bot.scanner import MarketScanner
from bot.state import get_state

log = get_logger("dashboard")


class Dashboard:
    def __init__(
        self,
        queue: OpportunityQueue,
        scanner: MarketScanner,
        executor: Executor,
    ) -> None:
        self._queue = queue
        self._scanner = scanner
        self._executor = executor
        self._risk = get_risk()
        self._state = get_state()
        self._rpc = get_rpc()
        self._tg = get_telegram()
        self._started_at = time.time()

    # ------------------------------------------------------------------
    # Panels
    # ------------------------------------------------------------------
    def _header(self) -> Panel:
        snap = self._risk.snapshot()
        kill = snap["kill_switch"]
        mode = CFG.mode.upper()
        mode_color = "yellow" if CFG.is_paper else "red"
        uptime = int(time.time() - self._started_at)
        text = Text.assemble(
            ("Polymarket Multi-Strategy Bot  ", "bold"),
            (f"[{mode}]", f"bold {mode_color}"),
            ("   equity=", "grey62"),
            (f"${snap['equity']:.2f}", "bold"),
            ("   pnl_day=", "grey62"),
            (f"{snap['pnl_day']:+.2f}", "green" if snap["pnl_day"] >= 0 else "red"),
            ("   pnl_month=", "grey62"),
            (f"{snap['pnl_month']:+.2f}", "green" if snap["pnl_month"] >= 0 else "red"),
            ("   dd=", "grey62"),
            ("YES" if snap["in_drawdown"] else "no", "red" if snap["in_drawdown"] else "green"),
            ("   kill=", "grey62"),
            ("ON" if kill else "off", "red" if kill else "green"),
            ("   uptime=", "grey62"),
            (f"{uptime // 3600:02d}h{(uptime % 3600) // 60:02d}m", "white"),
        )
        return Panel(Align.left(text), border_style="cyan")

    def _strategies_panel(self) -> Panel:
        t = Table(show_header=True, header_style="bold magenta", box=None, expand=True)
        t.add_column("Strategy")
        t.add_column("Status")
        t.add_column("Trades", justify="right")
        t.add_column("Wins", justify="right")
        t.add_column("Losses", justify="right")
        t.add_column("Win %", justify="right")
        t.add_column("PnL (USDC)", justify="right")

        snap = self._risk.snapshot()
        paused = snap["strategies_paused"]
        for name in sorted(CFG.strategies_enabled):
            s = self._state.get_stats(name)
            wr = s.win_rate * 100
            wr_color = "green" if wr >= 80 else "yellow" if wr >= 60 else "red"
            if name in paused:
                status = Text(f"paused {int(paused[name])}s", style="yellow")
            else:
                status = Text("active", style="green")
            t.add_row(
                name,
                status,
                str(s.trades),
                str(s.wins),
                str(s.losses),
                Text(f"{wr:.1f}%", style=wr_color) if s.trades else Text("-", style="grey62"),
                Text(
                    f"{s.pnl_usdc:+.2f}",
                    style="green" if s.pnl_usdc >= 0 else "red",
                ),
            )
        return Panel(t, title="Strategies", border_style="magenta")

    def _queue_panel(self) -> Panel:
        t = Table(show_header=True, header_style="bold cyan", box=None, expand=True)
        t.add_column("Prio", justify="right")
        t.add_column("Strategy")
        t.add_column("Market")
        t.add_column("Legs", justify="right")
        t.add_column("Edge %", justify="right")
        t.add_column("Conf.", justify="right")
        t.add_column("Age", justify="right")

        now = time.time()
        for opp in self._queue.snapshot()[:10]:
            age = int(now - opp.created_at)
            t.add_row(
                str(opp.priority),
                opp.strategy,
                opp.market_slug[:40],
                str(len(opp.legs)),
                f"{opp.expected_profit_pct * 100:+.2f}%",
                f"{opp.confidence:.2f}",
                f"{age}s",
            )
        if not len(self._queue):
            t.add_row("-", "-", "(empty)", "-", "-", "-", "-")
        return Panel(
            t,
            title=f"Opportunity Queue ({len(self._queue)})  inflight={self._executor.inflight}",
            border_style="cyan",
        )

    def _fills_panel(self) -> Panel:
        t = Table(show_header=True, header_style="bold green", box=None, expand=True)
        t.add_column("When")
        t.add_column("Strategy")
        t.add_column("Market")
        t.add_column("Status")
        t.add_column("PnL", justify="right")
        t.add_column("Reason")

        for f in self._state.recent_fills(limit=10):
            ts = datetime.fromtimestamp(
                f.get("executed_at", time.time()), tz=timezone.utc
            ).strftime("%H:%M:%S")
            status = f.get("status", "")
            style = {
                "simulated": "cyan",
                "filled": "green",
                "skipped": "yellow",
                "failed": "red",
            }.get(status, "white")
            pnl = float(f.get("pnl_usdc") or 0.0)
            t.add_row(
                ts,
                str(f.get("strategy", "")),
                str(f.get("market_id", ""))[:24],
                Text(status, style=style),
                Text(f"{pnl:+.2f}", style="green" if pnl >= 0 else "red"),
                str(f.get("reason", ""))[:30],
            )
        return Panel(t, title="Recent Fills", border_style="green")

    def _connectivity_panel(self) -> Panel:
        rpc_ok = self._rpc.is_connected()
        tg_ok = self._tg.enabled
        snap = self._scanner.snapshot
        scan_age = int(time.time() - snap.generated_at) if snap.generated_at else -1

        t = Table(box=None, expand=True, show_header=False)
        t.add_column("Key", style="grey62")
        t.add_column("Value")
        t.add_row(
            "RPC",
            Text(
                f"{self._rpc.active_endpoint.name} {'OK' if rpc_ok else 'DOWN'}",
                style="green" if rpc_ok else "red",
            ),
        )
        t.add_row(
            "Telegram",
            Text("connected" if tg_ok else "disabled", style="green" if tg_ok else "grey62"),
        )
        t.add_row(
            "Last scan",
            Text(
                f"{scan_age}s ago ({snap.scan_duration_seconds:.2f}s)",
                style="green" if 0 <= scan_age <= CFG.scan_interval * 2 else "yellow",
            ),
        )
        t.add_row("Markets tracked", str(len(snap.markets)))
        t.add_row("Arb candidates", str(len(snap.arbitrage_candidates)))
        t.add_row("Tail-end candidates", str(len(snap.tail_end_candidates)))
        t.add_row("Micro-spread candidates", str(len(snap.micro_spread_candidates)))
        t.add_row("Crypto 15m markets", str(len(snap.crypto_15m_markets)))
        t.add_row("API error streak", str(snap.generated_at and self._risk.snapshot()["api_error_streak"]))
        return Panel(t, title="Connectivity", border_style="blue")

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _render(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(self._header(), name="header", size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(name="left", ratio=3),
            Layout(self._connectivity_panel(), name="right", ratio=2),
        )
        layout["body"]["left"].split_column(
            Layout(self._strategies_panel(), name="strats"),
            Layout(self._queue_panel(), name="queue"),
            Layout(self._fills_panel(), name="fills"),
        )
        return layout

    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        if CFG.log_level == "DEBUG":
            # With DEBUG logs the Live view would flicker; just skip.
            log.info("[grey]Dashboard disabled at DEBUG log level.[/]")
            while True:
                await asyncio.sleep(3600)

        with Live(
            self._render(),
            console=CONSOLE,
            refresh_per_second=max(1, int(1 / CFG.dashboard_refresh)),
            screen=False,
        ) as live:
            while True:
                try:
                    live.update(self._render())
                except Exception as e:  # noqa: BLE001
                    log.debug(f"dashboard render failed: {e}")
                await asyncio.sleep(CFG.dashboard_refresh)
