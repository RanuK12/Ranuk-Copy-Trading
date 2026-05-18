"""Opportunity executor.

Consumes :class:`~bot.models.Opportunity` items from the
:class:`~bot.queue.OpportunityQueue`, runs them through the
:class:`~bot.risk.RiskManager`, then either:

* **paper mode** — prints a ``[SIMULADO]`` line, records a synthetic fill.
* **live  mode** — submits real orders through ``py-clob-client-v2``.

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

        # 2. Duplicate guard (per strategy + market) — only for BUYs
        is_sell = any(leg.side is Side.SELL for leg in opp.legs)
        if not is_sell and self._state.has_open_position(opp.strategy, opp.market_id):
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

        # 3b. Liquidity gate — refuse to enter a book where exiting would
        # be impossible. Only apply to single-leg BUYs (multi-leg arbitrage
        # already has its own depth logic upstream).
        if len(opp.legs) == 1 and opp.legs[0].side is Side.BUY:
            from bot.intelligence import orderbook_has_liquidity
            leg = opp.legs[0]
            book = await self._poly.get_order_book_async(leg.token_id)
            ok, liq_reason = orderbook_has_liquidity(
                book,
                desired_size_usdc=leg.size_usdc,
                side="BUY",
                max_spread=CFG.liquidity_max_spread,
                min_depth_multiplier=CFG.liquidity_min_depth_multiplier,
            )
            if not ok:
                log.info(f"{tag} -> [yellow]illiquid:[/] {liq_reason}")
                await self._record(
                    opp, status="skipped", reason=f"illiquid:{liq_reason}"
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
        # Paper PnL uses the strategy's expected edge rather than mark-to-market
        # live prices.  Mark-to-market creates false losses when the ask dips
        # below our entry — the real PnL only materialises at resolution.
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
                # Conservative paper PnL: expected edge × size × 0.5
                # This avoids the mark-to-market illusion while still
                # rewarding high-confidence opportunities.
                theoretical_pnl += leg.size_usdc * opp.expected_profit_pct * 0.5

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
            is_sell = any(leg.side is Side.SELL for leg in opp.legs)
            if is_sell:
                self._state.close_position(opp.strategy, opp.market_id, 0.0)
                self._risk.release_exposure(
                    opp.strategy, opp.market_id, sum(leg.size_usdc for leg in opp.legs)
                )
            else:
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
            # Log each failing leg so we know *why* it failed (size, price, etc.)
            for leg, r in zip(opp.legs, results):
                if isinstance(r, dict) and not r.get("success"):
                    log.warning(
                        f"{self._tag(opp)} -> leg failed: {r.get('error')}"
                    )
            await self._record(
                opp, status="rejected", reason="partial_fill", details=details
            )
            await self._tg.send(
                f"⚠️ Order rejected: {opp.strategy} / {opp.market_slug}\n"
                f"Filled {ok_count}/{len(opp.legs)} legs."
            )

    async def _send_leg(self, opp: Opportunity, leg: Leg) -> dict[str, Any]:
        """Send a single CLOB order. Returns a dict with success/response.

        Uses market orders (FOK) exclusively — py-clob-client-v2 limit-order
        amount rounding is incompatible with the V2 server validation for
        certain tick sizes.  Market orders route through
        ``get_market_order_amounts`` which produces the correct decimal
        precision.
        """
        try:
            from py_clob_client_v2.clob_types import (  # type: ignore
                MarketOrderArgs,
                OrderType,
            )
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"py-clob-client-v2 unavailable: {e}"}

        client = self._poly.clob()
        if client is None:
            return {"success": False, "error": "CLOB client not initialized"}

        side_str = "BUY" if leg.side is Side.BUY else "SELL"
        try:
            # Market orders: BUY -> amount in USDC, SELL -> amount in shares
            if leg.side is Side.BUY:
                amount = float(leg.size_usdc)
            else:
                px = max(leg.limit_price or opp.reference_price or 1e-6, 1e-6)
                amount = float(round(leg.size_usdc / px, 2))

            args = MarketOrderArgs(
                token_id=leg.token_id,
                amount=amount,
                side=side_str,
                order_type=OrderType.FOK,
            )
            resp = await asyncio.to_thread(
                client.create_and_post_market_order, args, order_type=OrderType.FOK
            )
            return {"success": True, "response": resp}
        except Exception as e:  # noqa: BLE001
            err_msg = str(e)
            if hasattr(e, "message"):
                err_msg = f"{err_msg} | msg={getattr(e, 'message')}"
            if hasattr(e, "response"):
                resp = getattr(e, "response")
                if resp is not None:
                    try:
                        err_msg = f"{err_msg} | body={resp.text}"
                    except Exception:  # noqa: BLE001
                        pass
            log.warning(
                f"[red]CLOB order failed[/] token={leg.token_id[:16]}... "
                f"side={leg.side.value} size={leg.size_usdc:.2f} price={leg.limit_price} -> {err_msg}"
            )
            return {"success": False, "error": err_msg}

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
        elif status == "rejected":
            # Business-level rejections (min size, closed market, etc.)
            # do NOT count against the API error streak.
            pass

    # ------------------------------------------------------------------
    def _tag(self, opp: Opportunity) -> str:
        return (
            f"[blue]{opp.strategy}[/] "
            f"market={opp.market_slug} "
            f"legs={len(opp.legs)} "
            f"ref={opp.reference_price} "
            f"edge={opp.expected_profit_pct*100:+.2f}%"
        )
