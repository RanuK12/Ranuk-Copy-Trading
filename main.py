"""Orchestrator for the Polymarket multi-strategy trading bot.

Responsibilities
----------------
* Wire the shared singletons (config, queue, scanner, executor, risk, state,
  telegram, RPC) together in the correct startup order.
* Launch one async task per enabled strategy that polls the scanner snapshot
  and pushes :class:`~bot.models.Opportunity` instances into the shared queue.
* Run scanner + executor + dashboard + heartbeat + telegram listener.
* Register /emergencystop, /resume, /status, /pnl commands on Telegram.
* Handle SIGINT/SIGTERM by cancelling tasks and persisting state.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from typing import Iterable

from bot.clients.rpc import get_rpc
from bot.clients.telegram import get_telegram
from bot.config import CFG
from bot.dashboard import Dashboard
from bot.executor import Executor
from bot.logger import get_logger
from bot.models import Opportunity
from bot.queue import OpportunityQueue
from bot.risk import get_risk
from bot.scanner import MarketScanner, get_scanner
from bot.state import get_state
from bot.strategies import REGISTRY as STRATEGY_REGISTRY
from bot.strategies.base import Strategy

log = get_logger("main")


# ---------------------------------------------------------------------------
# Strategy loop — one coroutine per enabled strategy
# ---------------------------------------------------------------------------
async def strategy_loop(
    strategy: Strategy,
    scanner: MarketScanner,
    queue: OpportunityQueue,
) -> None:
    log.info(f"[green]Strategy loop started:[/] {strategy.name}")
    # Stagger a bit so all strategies don't call generate() on the same scan.
    await asyncio.sleep(0.25)
    last_generated_at = 0.0
    while True:
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
            pushed = await queue.push(opp)
            if not pushed:
                log.debug(f"{strategy.name}: duplicate opp for {opp.market_slug}; skipped.")


# ---------------------------------------------------------------------------
# Heartbeat — periodic block number + risk snapshot line
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
        totals = {
            "fills": len(state.recent_fills(50)),
            "queue": len(queue),
            "equity": rsnap["equity"],
            "pnl_day": rsnap["pnl_day"],
            "kill": rsnap["kill_switch"],
        }
        log.info(
            f"[grey]heartbeat[/] block={block} "
            f"queue={totals['queue']} "
            f"fills={totals['fills']} "
            f"equity=${totals['equity']:.2f} "
            f"pnl_day={totals['pnl_day']:+.2f} "
            f"kill={'ON' if totals['kill'] else 'off'}"
        )
        await asyncio.sleep(CFG.heartbeat_interval)


# ---------------------------------------------------------------------------
# Telegram bindings
# ---------------------------------------------------------------------------
def _format_status() -> str:
    risk = get_risk()
    state = get_state()
    snap = risk.snapshot()
    lines = [
        f"*Mode*: {CFG.mode}",
        f"*Equity*: ${snap['equity']:.2f}  (peak ${snap['peak_equity']:.2f})",
        f"*PnL today*: {snap['pnl_day']:+.2f} USDC",
        f"*PnL month*: {snap['pnl_month']:+.2f} USDC",
        f"*Kill switch*: {'ON' if snap['kill_switch'] else 'off'}",
        "*Strategies*:",
    ]
    for name in sorted(CFG.strategies_enabled):
        s = state.get_stats(name)
        wr = f"{s.win_rate * 100:.1f}%" if s.trades else "-"
        lines.append(
            f"  • {name}: trades={s.trades} wr={wr} pnl={s.pnl_usdc:+.2f}"
        )
    return "\n".join(lines)


def wire_telegram_commands() -> None:
    tg = get_telegram()
    risk = get_risk()

    async def cmd_kill(_: str) -> None:
        risk.trigger_kill_switch("telegram:/emergencystop")
        await tg.send("🛑 Kill switch ACTIVATED. All new orders blocked.")

    async def cmd_resume(_: str) -> None:
        risk.release_kill_switch()
        await tg.send("✅ Trading resumed.")

    async def cmd_status(_: str) -> None:
        await tg.send_markdown(_format_status())

    async def cmd_pnl(_: str) -> None:
        s = risk.snapshot()
        await tg.send(
            f"Equity ${s['equity']:.2f} | "
            f"PnL day {s['pnl_day']:+.2f} | "
            f"PnL month {s['pnl_month']:+.2f}"
        )

    tg.on_command("emergencystop", cmd_kill)
    tg.on_command("stop", cmd_kill)
    tg.on_command("resume", cmd_resume)
    tg.on_command("start", cmd_resume)
    tg.on_command("status", cmd_status)
    tg.on_command("pnl", cmd_pnl)


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------
async def main() -> None:
    log.info(
        f"[bold]Polymarket Multi-Strategy Bot[/] starting | "
        f"mode={CFG.mode} | strategies={','.join(CFG.strategies_enabled)}"
    )

    # --- Pre-flight sanity ---------------------------------------------
    unknown = [s for s in CFG.strategies_enabled if s not in STRATEGY_REGISTRY]
    if unknown:
        log.error(f"[red]Unknown strategy names in STRATEGIES_ENABLED: {unknown}[/]")
        sys.exit(1)

    if not CFG.is_paper:
        if not CFG.poly_private_key or not CFG.poly_funder:
            log.error("[red]Live mode requires POLY_PRIVATE_KEY and POLY_FUNDER.[/]")
            sys.exit(1)

    # --- Wire singletons -----------------------------------------------
    queue = OpportunityQueue(maxsize=1024)
    scanner = get_scanner()
    executor = Executor(queue)
    tg = get_telegram()
    wire_telegram_commands()

    # --- Instantiate strategies ----------------------------------------
    strategies: list[Strategy] = []
    for name in CFG.strategies_enabled:
        strategies.append(STRATEGY_REGISTRY[name]())
    log.info(f"[green]Loaded {len(strategies)} strategies[/]: {[s.name for s in strategies]}")

    # --- Startup Telegram notice ---------------------------------------
    if tg.enabled:
        await tg.send(
            f"🤖 Bot online · mode={CFG.mode.upper()} · "
            f"strategies={len(strategies)} · "
            f"capital=${CFG.total_capital_usdc:.0f}"
        )

    # --- Launch tasks --------------------------------------------------
    tasks: list[asyncio.Task] = [
        asyncio.create_task(scanner.run_forever(), name="scanner"),
        asyncio.create_task(executor.run_forever(), name="executor"),
        asyncio.create_task(heartbeat(queue), name="heartbeat"),
        asyncio.create_task(tg.listen_for_commands(), name="telegram"),
    ]
    for strat in strategies:
        tasks.append(
            asyncio.create_task(
                strategy_loop(strat, scanner, queue), name=f"strat.{strat.name}"
            )
        )
    # Dashboard last (so all state is wired before first render)
    dashboard = Dashboard(queue, scanner, executor)
    tasks.append(asyncio.create_task(dashboard.run_forever(), name="dashboard"))

    # --- Shutdown handling ---------------------------------------------
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
    await stop

    log.info("[yellow]Shutdown signal received; cancelling tasks...[/]")
    if tg.enabled:
        await tg.send(f"🛑 Bot offline after {int(time.time())} (graceful).")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    get_state().save()
    log.info("[green]State persisted. Goodbye.[/]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
