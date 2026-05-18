"""Minimal FastAPI + WebSocket dashboard.

Enable with ``python main.py --dashboard web`` (or ``--dashboard web+tui``).
Serves a single HTML page with Chart.js plus a WebSocket endpoint that
pushes a JSON snapshot every second. Runs on ``0.0.0.0:8080`` by default;
override with ``WEB_HOST`` / ``WEB_PORT`` env vars.

Stateless: all data comes from the same singletons the TUI uses, so
enabling the web dashboard in addition to the TUI has no performance
cost beyond the WebSocket push.

Everything is optional — if FastAPI / uvicorn are missing, :func:`run_server`
returns immediately with a warning and the main orchestrator continues.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from bot.config import CFG
from bot.executor import Executor
from bot.logger import get_logger
from bot.queue import OpportunityQueue
from bot.risk import get_risk
from bot.scanner import MarketScanner
from bot.state import get_state

log = get_logger("web")

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response  # type: ignore
    from fastapi.responses import HTMLResponse  # type: ignore
    import uvicorn  # type: ignore
    _AVAILABLE = True
except Exception as e:  # noqa: BLE001
    log.debug(f"FastAPI/uvicorn unavailable ({e}); web dashboard disabled.")
    _AVAILABLE = False


_STATIC_DIR = Path(__file__).parent / "static"
_LIVE_POSITIONS: list[dict[str, Any]] = []


async def _poll_live_positions() -> None:
    from bot.clients.polymarket import get_poly
    poly = get_poly()
    while True:
        try:
            if not CFG.is_paper and CFG.poly_funder:
                positions = await asyncio.to_thread(poly.get_user_positions, CFG.poly_funder)
                global _LIVE_POSITIONS
                _LIVE_POSITIONS = positions or []
        except Exception as e:
            log.debug(f"Failed to fetch live positions: {e}")
        await asyncio.sleep(30)


def _snapshot_json(
    queue: OpportunityQueue,
    scanner: MarketScanner,
    executor: Executor,
) -> dict:
    risk = get_risk()
    state = get_state()
    rsnap = risk.snapshot()
    snap = scanner.snapshot

    # Build equity curve from recent fills (newest last).
    fills = list(reversed(state.recent_fills(limit=100)))
    equity_series: list[dict] = []
    running = 0.0
    for f in fills:
        running += float(f.get("pnl_usdc") or 0)
        equity_series.append(
            {
                "ts": float(f.get("executed_at") or time.time()),
                "equity": round(running, 4),
                "strategy": str(f.get("strategy") or ""),
            }
        )

    per_strat = []
    for name in sorted(CFG.strategies_enabled):
        s = state.get_stats(name)
        per_strat.append(
            {
                "name": name,
                "trades": s.trades,
                "wins": s.wins,
                "losses": s.losses,
                "pnl": round(s.pnl_usdc, 4),
                "win_rate": round(s.win_rate, 4),
                "profit_factor": round(min(s.profit_factor, 99.0), 2),
            }
        )

    # Internal bot positions (from state)
    bot_positions = []
    for strategy, positions in state.state.positions_by_strategy.items():
        for pos in positions:
            if not pos.get("open"):
                continue
            legs = pos.get("legs", [])
            entry_price = None
            token_id = None
            if legs and "price" in legs[0]:
                entry_price = legs[0].get("price")
                token_id = legs[0].get("token_id", "")[:16]
            bot_positions.append({
                "strategy": strategy,
                "market_id": pos.get("market_id", "")[:20],
                "size_usdc": pos.get("size_usdc", 0),
                "entry_price": entry_price,
                "opened_at": pos.get("opened_at", 0),
                "paper": pos.get("paper", False),
                "sl_price": round(entry_price * (1 - CFG.stop_loss_pct), 4) if entry_price else None,
                "tp_price": round(entry_price * (1 + CFG.take_profit_pct), 4) if entry_price else None,
            })

    return {
        "mode": CFG.mode,
        "ts": time.time(),
        "equity": round(rsnap["equity"], 4),
        "pnl_day": round(rsnap["pnl_day"], 4),
        "pnl_month": round(rsnap["pnl_month"], 4),
        "kill_switch": bool(rsnap["kill_switch"]),
        "in_drawdown": bool(rsnap["in_drawdown"]),
        "queue_depth": len(queue),
        "inflight": executor.inflight,
        "scanner": {
            "last_scan_age_s": int(time.time() - snap.generated_at) if snap.generated_at else None,
            "markets_tracked": len(snap.markets),
            "arb_candidates": len(snap.arbitrage_candidates),
            "tail_end_candidates": len(snap.tail_end_candidates),
            "micro_spread_candidates": len(snap.micro_spread_candidates),
            "crypto_15m": len(snap.crypto_15m_markets),
        },
        "strategies": per_strat,
        "equity_curve": equity_series,
        "recent_fills": state.recent_fills(limit=20),
        "open_positions": _LIVE_POSITIONS,
        "bot_positions": bot_positions,
        "risk_config": {
            "stop_loss_pct": CFG.stop_loss_pct,
            "take_profit_pct": CFG.take_profit_pct,
            "monitor_interval": CFG.position_monitor_interval,
            "min_entry_price": CFG.copy_min_entry_price,
        },
        "smart_wallets_count": len(CFG.smart_wallets),
        "wallet_address": CFG.poly_funder,
    }


def create_app(
    queue: OpportunityQueue,
    scanner: MarketScanner,
    executor: Executor,
):  # type: ignore[no-untyped-def]
    if not _AVAILABLE:
        return None

    app = FastAPI(title="Polymarket Bot Dashboard")

    @app.get("/", response_class=HTMLResponse)
    async def index(response: Response) -> str:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        return (_STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/api/snapshot")
    async def snapshot_endpoint(response: Response):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        return _snapshot_json(queue, scanner, executor)

    @app.websocket("/ws/dashboard")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                await websocket.send_text(
                    json.dumps(_snapshot_json(queue, scanner, executor), default=str)
                )
                await asyncio.sleep(1.0)
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.debug(f"websocket dropped: {e}")

    return app


async def run_server(
    queue: OpportunityQueue,
    scanner: MarketScanner,
    executor: Executor,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> None:
    """Async entry for the orchestrator. No-op if FastAPI isn't installed."""
    if not _AVAILABLE:
        log.warning(
            "Web dashboard requested but fastapi/uvicorn not installed. "
            "pip install fastapi uvicorn"
        )
        return
    host = host or os.getenv("WEB_HOST", "0.0.0.0")
    port = int(port or os.getenv("WEB_PORT", "8080"))
    app = create_app(queue, scanner, executor)
    if app is None:
        return
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    log.info(f"[green]Web dashboard:[/] http://{host}:{port}")
    try:
        task = asyncio.create_task(_poll_live_positions())
        await server.serve()
    except SystemExit as e:
        log.warning(f"Web dashboard exited (code={e.code}); bot continues.")
    except Exception as e:  # noqa: BLE001
        log.warning(f"Web dashboard crashed: {e}; bot continues.")
    finally:
        if 'task' in locals():
            task.cancel()
