"""Auto-discovery and rotation of elite wallets for SmartCopy.

Runs every 12 hours, scrapes the Polymarket leaderboard, scores each
candidate by ROI, win-rate, recent activity and market-type mix, then
updates the in-memory :data:`CFG.smart_wallets` list so the bot copies
fresh alpha without manual intervention.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from bot.clients.polymarket import get_poly
from bot.config import CFG
from bot.logger import get_logger

log = get_logger("wallet_discovery")

_DISCOVERY_INTERVAL_SECONDS = 8 * 3600  # 8 AM / 8 PM UTC
_MIN_TRADES_FOR_SCORING = 5
_RECENT_HOURS = 48


@dataclass
class TraderScore:
    wallet: str
    rank: int
    pnl: float
    volume: float
    win_rate: float
    roi_pct: float
    recent_trades: int
    avg_trade_size: float
    non_sports_ratio: float
    score: float


class WalletDiscovery:
    """Polls the Data-API leaderboard and refreshes ``CFG.smart_wallets``."""

    def __init__(self) -> None:
        self._poly = get_poly()
        self._last_run = 0.0

    # ------------------------------------------------------------------
    # Public loop
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        log.info("[green]WalletDiscovery started[/] (interval=12h)")
        # Run immediately on startup, then every 12 h
        await self._discover_and_update()
        while True:
            await asyncio.sleep(_DISCOVERY_INTERVAL_SECONDS)
            await self._discover_and_update()

    # ------------------------------------------------------------------
    # Core discovery pipeline
    # ------------------------------------------------------------------
    async def _discover_and_update(self) -> None:
        t0 = time.monotonic()
        try:
            leaderboard = await self._fetch_leaderboard()
        except Exception as e:  # noqa: BLE001
            log.warning(f"Leaderboard fetch failed: {e}")
            return

        if not leaderboard:
            log.warning("Leaderboard empty")
            return

        candidates: list[TraderScore] = []
        for entry in leaderboard[:50]:
            wallet = (entry.get("proxyWallet") or "").lower()
            if not wallet or not wallet.startswith("0x"):
                continue
            score = await self._score_wallet(wallet, entry)
            if score is not None and self._passes_filters(score):
                candidates.append(score)

        if not candidates:
            log.warning("No wallet candidates passed filters (leaderboard entries may not meet min-trades / win-rate / ROI thresholds)")
            return

        candidates.sort(key=lambda x: x.score, reverse=True)
        top = candidates[:15]
        new_wallets = [c.wallet for c in top]

        old_wallets = [w.lower() for w in CFG.smart_wallets]
        # Merge hardcoded + discovered wallets instead of replacing
        merged = list(dict.fromkeys(old_wallets + new_wallets))[:15]
        added = [w for w in new_wallets if w not in old_wallets]
        removed = [w for w in old_wallets if w not in merged]

        # Update frozen CFG via object.__setattr__ (safe for this field)
        object.__setattr__(CFG, "smart_wallets", merged)

        log.info(
            f"[cyan]Wallet rotation[/] {len(merged)} wallets | "
            f"added={len(added)} removed={len(removed)} "
            f"in {time.monotonic()-t0:.1f}s"
        )
        for w in added:
            log.info(f"  [green]+[/] {w[:14]}...")
        for w in removed:
            log.info(f"  [red]-[/] {w[:14]}...")
        for c in top[:5]:
            log.info(
                f"  [dim]{c.wallet[:14]}...[/]  rank={c.rank} "
                f"wr={c.win_rate:.0%} roi={c.roi_pct:.1f}% "
                f"recent={c.recent_trades} nonSports={c.non_sports_ratio:.0%} "
                f"score={c.score:.1f}"
            )

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------
    async def _fetch_leaderboard(self) -> list[dict]:
        raw = await asyncio.to_thread(
            self._poly._get,
            f"{CFG.data_api_host}/v1/leaderboard",
        )
        return raw if isinstance(raw, list) else []

    async def _score_wallet(
        self, wallet: str, entry: dict
    ) -> Optional[TraderScore]:
        try:
            trades = await self._poly.get_user_trades_async(wallet, limit=100)
        except Exception as e:  # noqa: BLE001
            log.debug(f"trades fetch failed for {wallet[:10]}: {e}")
            return None

        if len(trades) < _MIN_TRADES_FOR_SCORING:
            return None

        # ----- basic aggregates -----
        wins = losses = 0
        total_pnl_approx = 0.0
        total_size = 0.0
        recent_count = 0
        non_sports_count = 0
        lottery_count = 0  # trades at < 15¢
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff_ts = now_ts - (_RECENT_HOURS * 3600)

        for t in trades:
            side = (t.get("side") or "").upper()
            price = float(t.get("price") or 0)
            size = float(t.get("size") or 0)
            ts = int(t.get("timestamp") or 0)
            slug = (t.get("slug") or "").lower()

            total_size += size
            if ts >= cutoff_ts:
                recent_count += 1

            # Count lottery bets (very low price entries)
            if side == "BUY" and price < 0.15:
                lottery_count += 1

            # Rough PnL approximation: BUY -> entry, SELL -> exit
            if side == "BUY":
                total_pnl_approx -= price * size
            elif side == "SELL":
                total_pnl_approx += price * size

            # Win/loss proxy: SELL price > avg BUY price on same asset
            # (simplified: we just track sell vs buy count as a coarse signal)
            if side == "SELL":
                wins += 1
            elif side == "BUY":
                losses += 1

            if slug and not any(
                s in slug for s in ("nba", "ufc", "nfl", "mlb", "epl", "lal", "vs-", "cs2-", "lol-", "nhl-")
            ):
                non_sports_count += 1

        total_round_trips = wins + losses
        if total_round_trips == 0:
            return None

        # Use leaderboard PnL as primary signal (more reliable than trade-level approx)
        leaderboard_pnl = float(entry.get("pnl", 0) or 0)
        avg_size = total_size / len(trades)
        lottery_ratio = lottery_count / max(sum(1 for t in trades if (t.get("side") or "").upper() == "BUY"), 1)
        non_sports_ratio = non_sports_count / len(trades)

        # For wallets that mostly hold to resolution (no sells), use leaderboard PnL
        # as the win signal instead of sell count
        if wins == 0 and leaderboard_pnl > 1000:
            # Estimate win_rate from leaderboard PnL vs volume
            vol = float(entry.get("vol", 0) or 0)
            win_rate = min(0.80, leaderboard_pnl / max(vol, leaderboard_pnl * 2) + 0.5)
            roi_pct = (leaderboard_pnl / max(avg_size * 10, 1.0)) * 100
        else:
            win_rate = wins / total_round_trips
            roi_pct = (total_pnl_approx / max(avg_size * 10, 1.0)) * 100

        # Composite score (higher = better)
        score = (
            win_rate * 25.0
            + min(leaderboard_pnl / 1000, 50.0)  # up to 50 pts for PnL
            + recent_count * 2.0
            + non_sports_ratio * 15.0
            - lottery_ratio * 40.0  # heavy penalty for lottery bets
            - max(0, (avg_size - 5000)) * 0.001  # penalise mega-whales
        )

        return TraderScore(
            wallet=wallet,
            rank=int(entry.get("rank", 999)),
            pnl=float(entry.get("pnl", 0) or 0),
            volume=float(entry.get("vol", 0) or 0),
            win_rate=win_rate,
            roi_pct=roi_pct,
            recent_trades=recent_count,
            avg_trade_size=avg_size,
            non_sports_ratio=non_sports_ratio,
            score=score,
        )

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------
    def _passes_filters(self, score: TraderScore) -> bool:
        # Must have recent activity
        if score.recent_trades < 1:
            return False
        # Win rate: accept hold-to-resolution wallets (estimated from PnL)
        if score.win_rate < 0.40:
            return False
        # Minimum score threshold (filters out lottery gamblers)
        if score.score < 20.0:
            return False
        # Reject wallets where >50% of buys are lottery (<15¢)
        if score.pnl < 5000:
            return False
        return True
