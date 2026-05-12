"""Opportunity executor.

Consumes :class:`~bot.models.Opportunity` items from the
:class:`~bot.queue.OpportunityQueue`, runs them through the
:class:`~bot.risk.RiskManager`, then either:

* **paper mode** — prints a ``[SIMULADO]`` line, records a synthetic fill.
* **live  mode** — submits real orders through ``py-clob-client``.

Slippage guard: for single-leg opportunities with ``reference_price`` and
``max_slippage`` set, the executor re-fetches the live price immediately
before sending and aborts if it moved too far in the wrong direction.

All outcomes flow into :class:`~bot.state.StateStore` and the
:class:`~bot.risk.RiskManager` so win-rate / PnL / exposure stay accurate.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from bot.clients.polymarket import get_poly
from bot.clients.telegram import get_telegram
from bot.config import CFG
from bot.logger import get_logger
from bot.models import Fill, Leg, Opportunity, OrderKind, Side
from bot.queue import OpportunityQueue
from bot.risk import get_risk
from bot.state import get_state

log = get_logger("executor")


class Executor:
    """Single consumer of the opportunity queue."""

    def __init__(self, queue: OpportunityQueue) -> None:
        self._queue = queue
        self._risk = get_risk()
        self._state = get_state()
        self._poly = get_poly()
        self._tg = get_telegram()
        self._inflight = 0

    @property
    def inflight(self) -> int:
        return self._inflight

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        log.info(f"[green]Executor started[/] mode={CFG.mode}")
        while True:
            opp = await self._queue.pop(timeout=1.0)
            if opp is None:
                continue
            self._inflight += 1
            try:
                await self._execute(opp)
            except Exception as e:  # noqa: BLE001
                log.exception(f"executor error for {opp.strategy}: {e}")
            finally:
                self._inflight -= 1

    # ------------------------------------------------------------------
    # Execution pipeline
    # ------------------------------------------------------------------
    async def _execute(self, opp: Opportunity) -> None:
        tag = self._tag(opp)

        # 1. Risk gate
        ok, reason = self._risk.allow(opp)
        if not ok:
            log.info(f"{tag} -> [yellow]blocked:[/] {reason}")
            await self._record(opp, status="skipped", reason=reason)
            return

        # 2. Duplicate guard (per strategy + market)
        if self._state.has_open_position(opp.strategy, opp.market_id):
            log.info(f"{tag} -> [yellow]duplicate:[/] already positioned.")
            await self._record(opp, status="skipped", reason="duplicate")
            return

        # 3. Slippage guard for single-leg opportunities with a reference
        if (
            len(opp.legs) == 1
            and opp.reference_price is not None
            and opp.max_slippage is not None
        ):
            leg = opp.legs[0]
            current = await self._poly.get_price_async(leg.token_id, leg.side.value)
            if current is None:
                log.warning(f"{tag} -> [red]no live price; abort.[/]")
                await self._record(opp, status="skipped", reason="no_price")
                return
            if leg.side is Side.BUY:
                cap = opp.reference_price * (1 + opp.max_slippage)
                if current > cap:
                    log.warning(
                        f"{tag} -> [red]slippage exceeded[/] "
                        f"current={current:.4f} cap={cap:.4f}"
                    )
                    await self._record(
                        opp, status="skipped", reason="slippage_exceeded",
                        details={"live_price": current, "cap": cap},
                    )
                    return

        # 4. Reserve exposure up-front (released on failure)
        size_total = sum(leg.size_usdc for leg in opp.legs)
        self._risk.reserve_exposure(opp.strategy, opp.market_id, size_total)

        # 5. Dispatch
        try:
            if CFG.is_paper:
                await self._execute_paper(opp)
            else:
                await self._execute_live(opp)
        except Exception as e:  # noqa: BLE001
            log.exception(f"{tag} -> [red]execution crashed:[/] {e}")
            self._risk.release_exposure(opp.strategy, opp.market_id, size_total)
            await self._record(opp, status="failed", reason=str(e))

    # ------------------------------------------------------------------
    # Paper trading
    # ------------------------------------------------------------------
    async def _execute_paper(self, opp: Opportunity) -> None:
        # Theoretical fill price == the reference price the strategy emitted,
        # or the current best ask if unspecified.
        fills_detail: list[dict[str, Any]] = []
        theoretical_pnl = 0.0
        for leg in opp.legs:
            px = leg.limit_price or opp.reference_price
            if px is None:
                px = await self._poly.get_price_async(leg.token_id, leg.side.value) or 0.0
            fills_detail.append(
                {
                    "token_id": leg.token_id,
                    "side": leg.side.value,
                    "size_usdc": leg.size_usdc,
                    "kind": leg.kind.value,
                    "price": px,
                }
            )
            if leg.side is Side.BUY:
                # Theoretical P&L = edge * shares, where expected_profit_pct
                # captures the strategy's own estimate.
                shares = leg.size_usdc / px if px > 0 else 0
                theoretical_pnl += shares * px * opp.expected_profit_pct

        log.info(
            f"[cyan][SIMULADO][/] Estrategia: {opp.strategy} | "
            f"Mercado: {opp.market_slug} | "
            f"Legs: {len(opp.legs)} | "
            f"Entrada: {opp.reference_price!s} | "
            f"Salida teorica: {opp.expected_profit_pct*100:+.2f}% | "
            f"P&L: {theoretical_pnl:+.4f} USDC"
        )

        self._state.open_position(
            opp.strategy,
            opp.market_id,
            size_total := sum(leg.size_usdc for leg in opp.legs),
            extra={
                "legs": fills_detail,
                "paper": True,
                "expected_profit_pct": opp.expected_profit_pct,
            },
        )
        await self._record(
            opp,
            status="simulated",
            pnl=theoretical_pnl,
            details={"legs": fills_detail, "size_usdc": size_total},
        )

    # ------------------------------------------------------------------
    # Live trading
    # ------------------------------------------------------------------
    async def _execute_live(self, opp: Opportunity) -> None:
        # Dispatch all legs concurrently (arbitrage requires both fills).
        results = await asyncio.gather(
            *[self._send_leg(opp, leg) for leg in opp.legs],
            return_exceptions=True,
        )

        ok_count = sum(1 for r in results if isinstance(r, dict) and r.get("success"))
        all_ok = ok_count == len(opp.legs)

        details = {
            "legs": [
                (r if isinstance(r, dict) else {"error": str(r)}) for r in results
            ],
            "ok_count": ok_count,
            "leg_count": len(opp.legs),
        }

        if all_ok:
            log.info(f"{self._tag(opp)} -> [green]ORDER POSTED[/] legs={ok_count}")
            self._state.open_position(
                opp.strategy,
                opp.market_id,
                sum(leg.size_usdc for leg in opp.legs),
                extra=details,
            )
            await self._record(opp, status="filled", details=details)
        else:
            # For multi-leg opportunities, partial fill is dangerous
            # (one-legged arbitrage). Log + alert prominently.
            log.warning(
                f"{self._tag(opp)} -> [red]PARTIAL FILL[/] "
                f"{ok_count}/{len(opp.legs)} legs; manual review recommended."
            )
            # Release exposure for any leg that failed.
            for leg, r in zip(opp.legs, results):
                if not (isinstance(r, dict) and r.get("success")):
                    self._risk.release_exposure(
                        opp.strategy, opp.market_id, leg.size_usdc
                    )
            await self._record(
                opp, status="failed", reason="partial_fill", details=details
            )
            await self._tg.send(
                f"⚠️ Partial fill: {opp.strategy} / {opp.market_slug}\n"
                f"Filled {ok_count}/{len(opp.legs)} legs. Please review positions."
            )

    async def _send_leg(self, opp: Opportunity, leg: Leg) -> dict[str, Any]:
        """Send a single CLOB order. Returns a dict with success/response."""
        try:
            from py_clob_client.clob_types import (  # type: ignore
                MarketOrderArgs,
                OrderArgs,
                OrderType,
            )
            from py_clob_client.order_builder.constants import BUY, SELL  # type: ignore
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"py-clob-client unavailable: {e}"}

        client = self._poly.clob()
        if client is None:
            return {"success": False, "error": "CLOB client not initialized"}

        side_const = BUY if leg.side is Side.BUY else SELL
        try:
            if leg.kind is OrderKind.FOK:
                args = MarketOrderArgs(
                    token_id=leg.token_id,
                    amount=float(leg.size_usdc),
                    side=side_const,
                    order_type=OrderType.FOK,
                )
                signed = await asyncio.to_thread(client.create_market_order, args)
                resp = await asyncio.to_thread(client.post_order, signed, OrderType.FOK)
            else:  # LIMIT or GTC
                assert leg.limit_price is not None, "limit_price required"
                size_shares = leg.size_usdc / max(leg.limit_price, 1e-6)
                args = OrderArgs(
                    token_id=leg.token_id,
                    price=float(leg.limit_price),
                    size=float(round(size_shares, 4)),
                    side=side_const,
                )
                signed = await asyncio.to_thread(client.create_order, args)
                order_type = OrderType.GTC if leg.kind is OrderKind.GTC else OrderType.GTC
                resp = await asyncio.to_thread(client.post_order, signed, order_type)
            return {"success": True, "response": resp}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Shared: record outcome + alert
    # ------------------------------------------------------------------
    async def _record(
        self,
        opp: Opportunity,
        *,
        status: str,
        pnl: float = 0.0,
        reason: str = "",
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        fill = Fill(
            opportunity_id=opp.id,
            strategy=opp.strategy,
            market_id=opp.market_id,
            status=status,
            pnl_usdc=pnl,
            reason=reason,
            details=details or {},
        )
        self._state.register_fill(fill)
        if status in ("filled", "simulated"):
            self._risk.register_api_success()
            self._risk.register_fill(opp.strategy, pnl)
            await self._tg.send(
                f"{'💸 [SIMULADO]' if status == 'simulated' else '✅ FILLED'} "
                f"{opp.strategy} / {opp.market_slug}\n"
                f"PnL: {pnl:+.4f} USDC | conf={opp.confidence:.2f}"
            )
        elif status == "failed":
            self._risk.register_api_error()

    # ------------------------------------------------------------------
    def _tag(self, opp: Opportunity) -> str:
        return (
            f"[blue]{opp.strategy}[/] "
            f"market={opp.market_slug} "
            f"legs={len(opp.legs)} "
            f"ref={opp.reference_price} "
            f"edge={opp.expected_profit_pct*100:+.2f}%"
        )
