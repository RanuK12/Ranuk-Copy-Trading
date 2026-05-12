"""Orchestrator for the Polymarket multi-strategy trading bot (v3).

Entry modes
-----------
* ``python main.py``                           – full bot, Textual TUI (default).
* ``python main.py --dashboard tui|web|none``  – pick dashboard.
* ``python main.py --dashboard web+tui``       – both at once.
* ``python main.py --setup-wallet``            – wallet wizard (Tier 1/3).
* ``python main.py --command "status"``        – one-shot CLI, no loop.
* ``python main.py --metrics``                 – enable Prometheus on :9090.
* ``python main.py --replay logs/session.jsonl``  – stub for session replay.

The orchestrator is intentionally thin: wires singletons, launches tasks,
and exits cleanly. All business logic lives in the bot.* modules.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from typing import Iterable

from bot.clients.rpc import get_rpc
from bot.clients.telegram import get_telegram
from bot.config import CFG
from bot.core.budget import current_profile, filter_strategies
from bot.core.config_watcher import ConfigWatcher
from bot.executor import Executor
from bot.logger import get_logger
from bot.models import Opportunity
from bot.monitoring.commands import get_controller, get_processor
from bot.monitoring.notifications import NotificationRouter
from bot.queue import OpportunityQueue
from bot.risk import get_risk
from bot.scanner import MarketScanner, get_scanner
from bot.state import get_state
from bot.strategies import REGISTRY as STRATEGY_REGISTRY
from bot.strategies.base import Strategy

log = get_logger("main")


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------
def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polymarket-bot")
    p.add_argument(
        "--dashboard",
        default="tui",
        help="Dashboard flavour: tui (default), web, tui+web, none.",
    )
    p.add_argument(
        "--command",
        metavar="CMD",
        help='Run one command (e.g. "status", "pnl week") and exit.',
    )
    p.add_argument(
        "--setup-wallet",
        action="store_true",
        help="Launch interactive wallet setup wizard and exit.",
    )
    p.add_argument(
        "--metrics",
        action="store_true",
        help="Start Prometheus /metrics on :9090.",
    )
    p.add_argument(
        "--replay",
        metavar="PATH",
        help="Replay a session log (currently a stub; main is unaffected).",
    )
    p.add_argument(
        "--web-port",
        type=int,
        default=None,
        help="Override WEB_PORT for the web dashboard.",
    )
    return p


# ---------------------------------------------------------------------------
# Strategy fan-out
# ---------------------------------------------------------------------------
async def strategy_loop(
    strategy: Strategy,
    scanner: MarketScanner,
    queue: OpportunityQueue,
) -> None:
    controller = get_controller()
    log.info(f"[green]Strategy loop started:[/] {strategy.name}")
    await asyncio.sleep(0.25)  # stagger
    last_generated_at = 0.0
    while True:
        if not controller.is_enabled(strategy.name):
            await asyncio.sleep(1.0)
            continue
        snap = scanner.snapshot
        if snap.generated_at <= last_generated_at:
            await asyncio.sleep(min(1.0, CFG.scan_interval / 4))
            continue
        last_generated_at = snap.generated_at
        try:
            opps: Iterable[Opportunity] = await strategy.generate(snap) or []
        except Exception as e:  # noqa: BLE001
            log.exception(f"{strategy.name}.generate crashed: {e}")
            await asyncio.sleep(CFG.scan_interval)
            continue
        for opp in opps:
            await queue.push(opp)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
async def heartbeat(queue: OpportunityQueue) -> None:
    rpc = get_rpc()
    risk = get_risk()
    state = get_state()
    while True:
        try:
            block = rpc.block_number()
        except Exception as e:  # noqa: BLE001
            block = f"err({e})"
        rsnap = risk.snapshot()
        log.info(
            f"[grey]heartbeat[/] block={block} "
            f"queue={len(queue)} "
            f"fills={len(state.recent_fills(50))} "
            f"equity=${rsnap['equity']:.2f} "
            f"pnl_day={rsnap['pnl_day']:+.2f} "
            f"kill={'ON' if rsnap['kill_switch'] else 'off'}"
        )
        await asyncio.sleep(CFG.heartbeat_interval)


# ---------------------------------------------------------------------------
# Telegram wiring (now routed through the command processor)
# ---------------------------------------------------------------------------
def wire_telegram_commands() -> None:
    tg = get_telegram()
    if not tg.enabled:
        return
    processor = get_processor()

    async def _handler_factory(cmd: str):
        async def _run(arg: str) -> None:
            raw = f"{cmd} {arg}".strip() if arg else cmd
            result = processor.dispatch(raw)
            await tg.send(result.as_text())
        return _run

    async def _wire() -> None:
        for c in ("status", "pnl", "strategies", "exposure", "help", "budget"):
            tg.on_command(c, await _handler_factory(c))

        async def stop_cmd(_: str) -> None:
            get_risk().trigger_kill_switch("telegram:/emergencystop")
            await tg.send("🛑 Kill switch ACTIVATED.")

        async def resume_cmd(arg: str) -> None:
            raw = "resume " + arg if arg else "resume"
            result = processor.dispatch(raw)
            await tg.send(result.as_text())

        tg.on_command("emergencystop", stop_cmd)
        tg.on_command("stop", stop_cmd)
        tg.on_command("resume", resume_cmd)
        tg.on_command("start", resume_cmd)

    asyncio.get_event_loop().create_task(_wire())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def run_bot(args: argparse.Namespace) -> int:
    # --- Budget profile banner ----------------------------------------
    profile = current_profile()
    log.info(profile.describe())

    # --- Strategy filtering by budget profile -------------------------
    requested = CFG.strategies_enabled
    allowed, dropped = filter_strategies(requested)
    for name in dropped:
        log.warning(
            f"[yellow]Budget tier '{profile.tier}' does not support '{name}'; "
            f"dropping it for this session.[/]"
        )
    enabled_strategies = allowed

    unknown = [s for s in enabled_strategies if s not in STRATEGY_REGISTRY]
    if unknown:
        log.error(f"[red]Unknown strategy names: {unknown}[/]")
        return 1

    if not CFG.is_paper:
        if not CFG.poly_private_key and not _has_encrypted_wallet():
            log.error(
                "[red]Live mode requires a configured wallet. Run "
                "`python main.py --setup-wallet` or set POLY_PRIVATE_KEY.[/]"
            )
            return 1

    # --- Singletons ---------------------------------------------------
    queue = OpportunityQueue(maxsize=1024)
    scanner = get_scanner()
    executor = Executor(queue)
    notifier = NotificationRouter()

    # Controller: start only the allowed strategies as enabled
    controller = get_controller()
    for name in list(controller.snapshot()):
        controller.set_enabled(name, name in enabled_strategies)

    # Telegram is optional now
    get_telegram()
    wire_telegram_commands()

    # Live-reload config (optional)
    watcher = ConfigWatcher()
    watcher.start()

    # Prometheus metrics (optional)
    if args.metrics:
        from bot.monitoring import metrics as M
        M.start(9090)

    # --- Strategy instances ------------------------------------------
    strategies: list[Strategy] = [STRATEGY_REGISTRY[name]() for name in enabled_strategies]
    log.info(
        f"[green]Loaded {len(strategies)} strategies:[/] {[s.name for s in strategies]}"
    )
    await notifier.bot_started(CFG.mode, CFG.total_capital_usdc)

    # --- Async tasks --------------------------------------------------
    session_start = time.time()
    tasks: list[asyncio.Task] = [
        asyncio.create_task(scanner.run_forever(), name="scanner"),
        asyncio.create_task(executor.run_forever(), name="executor"),
        asyncio.create_task(heartbeat(queue), name="heartbeat"),
    ]
    tg = get_telegram()
    if tg.enabled:
        tasks.append(asyncio.create_task(tg.listen_for_commands(), name="telegram"))
    for strat in strategies:
        tasks.append(
            asyncio.create_task(strategy_loop(strat, scanner, queue), name=f"strat.{strat.name}")
        )

    # --- Dashboard(s) -------------------------------------------------
    flavour = args.dashboard.lower()
    want_tui = "tui" in flavour
    want_web = "web" in flavour

    if want_web:
        try:
            from bot.web.server import run_server as run_web
            tasks.append(
                asyncio.create_task(
                    run_web(queue, scanner, executor, port=args.web_port),
                    name="web",
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"Web dashboard could not start: {e}")

    if want_tui:
        try:
            from bot.monitoring.tui_app import is_available, run_tui
            if is_available():
                # The TUI owns the event loop while it runs; when it exits
                # we trigger a graceful shutdown by cancelling the other tasks.
                tui_task = asyncio.create_task(run_tui(queue, scanner, executor), name="tui")
                tasks.append(tui_task)
            else:
                log.warning("Textual not available; falling back to log-only mode.")
        except Exception as e:  # noqa: BLE001
            log.warning(f"TUI could not start: {e}")

    # --- Shutdown handling -------------------------------------------
    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _on_signal() -> None:
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows

    # If the TUI exits, treat it as a stop signal.
    async def _watch_tui_exit() -> None:
        if not want_tui:
            return
        for t in tasks:
            if t.get_name() == "tui":
                try:
                    await t
                finally:
                    if not stop.done():
                        stop.set_result(None)
                return

    watcher_task = asyncio.create_task(_watch_tui_exit())

    await stop

    log.info("[yellow]Shutdown signal received; cancelling tasks...[/]")
    for t in tasks + [watcher_task]:
        t.cancel()
    await asyncio.gather(*tasks, *[watcher_task], return_exceptions=True)

    # Persist + generate report ---------------------------------------
    get_state().save()
    try:
        from bot.monitoring.log_analyzer import generate as gen_report
        gen_report(session_start)
    except Exception as e:  # noqa: BLE001
        log.debug(f"session report generation skipped: {e}")

    try:
        watcher.stop()
    except Exception:  # noqa: BLE001
        pass

    log.info("[green]Bye.[/]")
    return 0


def _has_encrypted_wallet() -> bool:
    try:
        from bot.wallet.secure_key import SecureKey
        return SecureKey.stored_address() is not None
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
def main() -> int:
    args = build_argparser().parse_args()

    # --setup-wallet: synchronous wizard
    if args.setup_wallet:
        from bot.wallet.wizard import run as run_wizard
        return run_wizard()

    # --command: one-shot CLI
    if args.command:
        from bot.monitoring.cli import run as run_cli
        return run_cli(args.command)

    # --replay: stub
    if args.replay:
        log.warning(
            "--replay is a stub in this release. Session replay is tracked "
            "in bot/monitoring/log_analyzer.py for a future PR."
        )
        return 0

    try:
        return asyncio.run(run_bot(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
