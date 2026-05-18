"""Position Monitor — continuous Stop-Loss / Take-Profit enforcement.

Scans all open positions every ``POSITION_MONITOR_INTERVAL`` seconds,
fetches live prices, and triggers sells when:
  - Price drops below entry * (1 - STOP_LOSS_PCT)   → Stop-Loss
  - Price rises above entry * (1 + TAKE_PROFIT_PCT) → Take-Profit
  - Trailing stop: once position is up 10%, SL moves to breakeven.
    Once up 20%, SL locks in +10%.

Also drains "agonizing" positions (avg_price was healthy, cur_price
collapsed to near-zero) — these otherwise sit open forever waiting for
Polymarket resolution and the SL never fires. We try to SELL them
whenever there is still some residual bid so we recover a few cents.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from bot.clients.polymarket import get_poly
from bot.clients.telegram import get_telegram
from bot.config import CFG
from bot.logger import get_logger
from bot.state import get_state

log = get_logger("position_monitor")


# Threshold below which a market is considered "agonizing" (the BUY-side
# of a losing binary is tanking toward zero).
_AGONIZING_PRICE = 0.01
# Only attempt forced exit once per market every N seconds.
_FORCED_EXIT_COOLDOWN = 120.0


class PositionMonitor:
    """Watches open positions and exits on SL/TP triggers."""

    def __init__(self) -> None:
        self._poly = get_poly()
        self._state = get_state()
        self._tg = get_telegram()
        # Track highest price seen per market for trailing stop
        self._high_water: dict[str, float] = {}
        # Track last forced-exit attempt to avoid spamming the CLOB
        self._last_forced_exit: dict[str, float] = {}

    async def run_forever(self) -> None:
        log.info(
            f"[green]PositionMonitor started[/] "
            f"SL={CFG.stop_loss_pct:.0%} TP={CFG.take_profit_pct:.0%} "
            f"interval={CFG.position_monitor_interval}s"
        )
        # Sync capital from Polymarket at startup so the bot's idea of
        # "available" matches reality (prevents stale exposure limits).
        await self._sync_capital_from_polymarket()
        loop = 0
        while True:
            await asyncio.sleep(CFG.position_monitor_interval)
            loop += 1
            try:
                await self._sync_live_positions()
                await self._check_all_positions()
                # Re-sync capital every 5 minutes so max_exposure caps stay real.
                if loop % max(1, int(300 / max(1, CFG.position_monitor_interval))) == 0:
                    await self._sync_capital_from_polymarket()
            except Exception as e:  # noqa: BLE001
                log.exception(f"PositionMonitor error: {e}")

    async def _sync_capital_from_polymarket(self) -> None:
        """Update CFG.total_capital_usdc = USDC available + open position value.

        Prevents the bot from believing it has more headroom than reality
        once we've taken losses. Uses the CLOB client's balance endpoint
        when available, falling back to the data-api value endpoint.
        """
        if CFG.is_paper or not CFG.poly_funder:
            return
        try:
            value = await asyncio.to_thread(self._poly.get_portfolio_value, CFG.poly_funder)
        except Exception as e:  # noqa: BLE001
            log.debug(f"capital sync failed: {e}")
            return
        if value is None or value <= 0:
            return
        old = CFG.total_capital_usdc
        # object.__setattr__ because CFG is frozen dataclass
        object.__setattr__(CFG, "total_capital_usdc", round(value, 2))
        if abs(old - value) >= 0.10:
            log.info(
                f"[cyan]capital sync:[/] ${old:.2f} -> ${value:.2f} "
                f"(delta {value-old:+.2f})"
            )
            # Refresh budget profile so exposure caps track reality.
            try:
                from bot.core.budget import refresh_profile
                refresh_profile(value)
            except Exception:  # noqa: BLE001
                pass
            # Update RiskManager peak + current equity floor.
            try:
                from bot.risk import get_risk
                rs = get_risk().state
                rs.current_equity = max(rs.current_equity, value)
                rs.peak_equity = max(rs.peak_equity, value)
            except Exception:  # noqa: BLE001
                pass

    async def _sync_live_positions(self) -> None:
        """Fetch real positions from Polymarket and log portfolio state."""
        if CFG.is_paper or not CFG.poly_funder:
            return
        try:
            positions = await asyncio.to_thread(
                self._poly.get_user_positions, CFG.poly_funder
            )
            if not positions:
                return

            total_value = 0.0
            for p in positions:
                size = float(p.get("size") or 0)
                cur = float(p.get("curPrice") or 0)
                total_value += size * cur

            log.info(
                f"[dim]portfolio sync:[/] {len(positions)} positions | "
                f"value=${total_value:.4f} | "
                f"available=${max(0.0, CFG.total_capital_usdc - total_value):.2f}"
            )
        except Exception as e:  # noqa: BLE001
            log.debug(f"portfolio sync failed: {e}")

    async def _check_all_positions(self) -> None:
        # Check internal bot positions (paper + live with token_id)
        state = self._state.state
        for strategy, positions in list(state.positions_by_strategy.items()):
            for pos in positions:
                if not pos.get("open"):
                    continue
                # Max-hold timer: force exit on positions that have been
                # sitting open longer than CFG.max_hold_seconds regardless
                # of SL/TP — stops "zombie" positions from tying up capital.
                from bot.intelligence import position_should_force_exit
                if position_should_force_exit(
                    opened_at=pos.get("opened_at"),
                    max_hold_seconds=CFG.max_hold_seconds,
                ):
                    token_id = self._get_token_id(pos)
                    entry_price = self._get_entry_price(pos)
                    if token_id and entry_price:
                        current_price = (
                            await self._poly.get_price_async(token_id, "SELL")
                        ) or 0.0
                        log.warning(
                            f"[yellow]⏰ MAX-HOLD[/] {strategy} "
                            f"market={pos.get('market_id','')[:16]}... "
                            f"age>{CFG.max_hold_seconds/3600:.1f}h "
                            f"entry={entry_price:.4f} now={current_price:.4f}"
                        )
                        await self._exit_position(
                            strategy, pos, token_id, entry_price,
                            current_price, "max_hold"
                        )
                        continue
                await self._evaluate_position(strategy, pos)

        # Check real Polymarket positions for SL/TP
        if not CFG.is_paper and CFG.poly_funder:
            await self._check_live_polymarket_positions()

    async def _evaluate_position(self, strategy: str, pos: dict[str, Any]) -> None:
        entry_price = self._get_entry_price(pos)
        token_id = self._get_token_id(pos)
        if not token_id or not entry_price or entry_price <= 0:
            return

        current_price = await self._poly.get_price_async(token_id, "SELL")
        if current_price is None:
            return

        market_id = pos.get("market_id", "")
        pnl_pct = (current_price - entry_price) / entry_price

        # Update high water mark for trailing stop
        hw = self._high_water.get(market_id, entry_price)
        if current_price > hw:
            hw = current_price
            self._high_water[market_id] = hw

        # Calculate dynamic SL based on trailing logic
        dynamic_sl = self._calc_trailing_sl(entry_price, hw)

        # Stop-Loss (trailing or fixed)
        if current_price <= dynamic_sl:
            log.warning(
                f"[red]🛑 STOP-LOSS[/] {strategy} market={market_id[:16]}... "
                f"entry={entry_price:.4f} high={hw:.4f} now={current_price:.4f} "
                f"sl_level={dynamic_sl:.4f} pnl={pnl_pct:+.1%}"
            )
            await self._exit_position(strategy, pos, token_id, entry_price, current_price, "stop_loss")
            self._high_water.pop(market_id, None)
            return

        # Take-Profit
        if pnl_pct >= CFG.take_profit_pct:
            log.info(
                f"[green]🎯 TAKE-PROFIT[/] {strategy} market={market_id[:16]}... "
                f"entry={entry_price:.4f} now={current_price:.4f} pnl={pnl_pct:+.1%}"
            )
            await self._exit_position(strategy, pos, token_id, entry_price, current_price, "take_profit")
            self._high_water.pop(market_id, None)
            return

    def _calc_trailing_sl(self, entry_price: float, high_water: float) -> float:
        """Calculate trailing stop-loss level.

        - Default: entry * (1 - SL_PCT)
        - If position went up 10%+: SL moves to breakeven (entry price)
        - If position went up 20%+: SL locks at entry * 1.10 (+10% profit)
        - If position went up 30%+: SL locks at entry * 1.20 (+20% profit)
        """
        gain_from_entry = (high_water - entry_price) / entry_price

        if gain_from_entry >= 0.30:
            return entry_price * 1.20  # Lock 20% profit
        elif gain_from_entry >= 0.20:
            return entry_price * 1.10  # Lock 10% profit
        elif gain_from_entry >= 0.10:
            return entry_price  # Breakeven
        else:
            return entry_price * (1 - CFG.stop_loss_pct)  # Fixed SL

    async def _exit_position(
        self,
        strategy: str,
        pos: dict[str, Any],
        token_id: str,
        entry_price: float,
        current_price: float,
        reason: str,
    ) -> None:
        market_id = pos.get("market_id", "")
        size_usdc = pos.get("size_usdc", 0.0)
        shares = size_usdc / entry_price if entry_price > 0 else 0.0

        if CFG.is_paper:
            # Paper exit: use a sensible fallback if cur_price is 0 / None.
            # For tail_end & smart_copy we approximate the "realised" PnL
            # as theoretical upside × 0.5 to avoid 100% losses from phantom
            # cur_price=0. For max_hold reason, settle at entry (breakeven).
            if current_price <= 0:
                if reason == "max_hold":
                    current_price = entry_price  # breakeven on time-out
                else:
                    current_price = entry_price * 0.5  # worst case
            pnl = (current_price - entry_price) * shares
            log.info(f"[cyan][SIMULADO SELL][/] {reason} pnl={pnl:+.4f} USDC")
            self._state.close_position(strategy, market_id, pnl)
            # Free up exposure so subsequent opportunities in this market/strategy
            # can be taken. Without this, paper positions permanently consume
            # the strategy/market caps and the bot stops trading.
            try:
                from bot.risk import get_risk
                get_risk().release_exposure(strategy, market_id, size_usdc)
            except Exception:  # noqa: BLE001
                pass
            try:
                from bot.risk import get_risk
                get_risk().register_fill(strategy, pnl)
            except Exception:  # noqa: BLE001
                pass
            await self._tg.send(
                f"📄 [PAPER] {reason.upper()} | {strategy}\n"
                f"Entry: {entry_price:.4f} → Exit: {current_price:.4f}\n"
                f"PnL: {pnl:+.4f} USDC"
            )
        else:
            result = await self._poly.sell_position_async(token_id, shares)
            if result.get("success"):
                pnl = (current_price - entry_price) * shares
                self._state.close_position(strategy, market_id, pnl)
                log.info(f"[green]SELL executed[/] {reason} pnl={pnl:+.4f}")
                await self._tg.send(
                    f"{'🛑' if reason == 'stop_loss' else '🎯'} {reason.upper()} | {strategy}\n"
                    f"Entry: {entry_price:.4f} → Exit: {current_price:.4f}\n"
                    f"PnL: {pnl:+.4f} USDC"
                )
            else:
                log.error(f"[red]SELL FAILED[/] {reason}: {result.get('error')}")
                await self._tg.send(
                    f"⚠️ SELL FAILED ({reason}) | {strategy}\n"
                    f"Error: {result.get('error')}"
                )

    def _get_entry_price(self, pos: dict[str, Any]) -> float | None:
        """Extract entry price from position legs data."""
        legs = pos.get("legs", [])
        if not legs:
            return None
        first_leg = legs[0]
        if "price" in first_leg:
            return float(first_leg["price"])
        return None

    def _get_token_id(self, pos: dict[str, Any]) -> str | None:
        """Extract token_id from position legs data."""
        legs = pos.get("legs", [])
        if not legs:
            return None
        first_leg = legs[0]
        if "token_id" in first_leg:
            return str(first_leg["token_id"])
        return None

    async def _check_live_polymarket_positions(self) -> None:
        """Monitor real Polymarket positions for SL/TP based on API data."""
        try:
            positions = await asyncio.to_thread(
                self._poly.get_user_positions, CFG.poly_funder
            )
        except Exception:  # noqa: BLE001
            return
        if not positions:
            return

        for p in positions:
            avg_price = float(p.get("avgPrice") or 0)
            cur_price = float(p.get("curPrice") or 0)
            size = float(p.get("size") or 0)
            token_id = p.get("asset", "")
            title = (p.get("title") or "?")[:40]

            if not avg_price or not token_id or size <= 0:
                continue

            # --- Agonizing position drain ----------------------------------
            # A BUY-side position bought above ~5¢ that has collapsed to <1¢
            # will never fire the % stop-loss (it already went through it)
            # and its residual value is usually non-zero. Try to dump it
            # while there is still a counter-bid.
            if cur_price < _AGONIZING_PRICE and avg_price >= 0.05:
                now = time.time()
                last = self._last_forced_exit.get(token_id, 0.0)
                if now - last < _FORCED_EXIT_COOLDOWN:
                    continue
                self._last_forced_exit[token_id] = now
                potential_recovery = cur_price * size
                log.warning(
                    f"[red]☠️  AGONIZING[/] {title} "
                    f"entry={avg_price:.4f} now={cur_price:.6f} "
                    f"size={size:.2f} recovery≈${potential_recovery:.3f}"
                )
                result = await self._poly.sell_position_async(token_id, size)
                if result.get("success"):
                    log.info(
                        f"[yellow]AGONIZING SELL OK[/] {title} "
                        f"recovered≈${potential_recovery:.3f}"
                    )
                    await self._tg.send(
                        f"☠️ FORCED EXIT | {title}\n"
                        f"Entry: ${avg_price:.4f} → Exit: ${cur_price:.6f}\n"
                        f"Recovered: ~${potential_recovery:.3f}"
                    )
                else:
                    err = str(result.get("error", ""))[:80]
                    log.debug(f"AGONIZING SELL failed (likely no bid) for {title}: {err}")
                continue

            # Skip dead positions (market fully resolved; nothing to sell).
            if cur_price < 0.0005:
                continue

            pnl_pct = (cur_price - avg_price) / avg_price

            # SL trigger on live positions
            if pnl_pct <= -CFG.stop_loss_pct:
                log.warning(
                    f"[red]🛑 LIVE SL[/] {title} "
                    f"entry={avg_price:.4f} now={cur_price:.6f} pnl={pnl_pct:+.1%}"
                )
                result = await self._poly.sell_position_async(token_id, size)
                if result.get("success"):
                    pnl = (cur_price - avg_price) * size
                    log.info(f"[green]LIVE SELL OK[/] {title} pnl={pnl:+.4f}")
                    await self._tg.send(
                        f"🛑 LIVE STOP-LOSS | {title}\n"
                        f"Entry: ${avg_price:.4f} → Exit: ${cur_price:.6f}\n"
                        f"PnL: ${pnl:+.4f}"
                    )
                else:
                    log.debug(f"LIVE SELL failed for {title}: {result.get('error', '')[:50]}")

            # TP trigger on live positions
            elif pnl_pct >= CFG.take_profit_pct:
                log.info(
                    f"[green]🎯 LIVE TP[/] {title} "
                    f"entry={avg_price:.4f} now={cur_price:.4f} pnl={pnl_pct:+.1%}"
                )
                result = await self._poly.sell_position_async(token_id, size)
                if result.get("success"):
                    pnl = (cur_price - avg_price) * size
                    log.info(f"[green]LIVE SELL OK[/] {title} pnl={pnl:+.4f}")
                    await self._tg.send(
                        f"🎯 LIVE TAKE-PROFIT | {title}\n"
                        f"Entry: ${avg_price:.4f} → Exit: ${cur_price:.4f}\n"
                        f"PnL: ${pnl:+.4f}"
                    )
                else:
                    log.debug(f"LIVE SELL failed for {title}: {result.get('error', '')[:50]}")
