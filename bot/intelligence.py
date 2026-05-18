"""Intelligent entry filters — the guardrails that separate "smart" trades
from "chasing a loser".

These helpers are called by strategies *before* pushing an opportunity onto
the queue. They embed the lessons learnt from the Counter-Strike / Arizona
Diamondbacks drawdowns:

* ``is_live_sports_event``:  refuses markets that look like an in-progress
  game (short slug pattern + high volatility + resolves within hours).
* ``is_wallet_panic_selling``: inspects the recent trades of a "smart"
  wallet to detect a liquidation cascade; if true, we must NOT treat the
  wallet's trades as alpha signal.
* ``orderbook_has_liquidity``: checks best-bid / best-ask depth and
  spread so we don't enter a market where exiting is impossible.
* ``risk_adjusted_edge_ok``: enforces upside / downside >= min ratio.

All functions are defensive — when in doubt (missing data, API hiccup)
they return the *conservative* answer (reject the trade).
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from bot.config import CFG
from bot.logger import get_logger

log = get_logger("intelligence")


# ---------------------------------------------------------------------------
# 1. Sports / live-event detection
# ---------------------------------------------------------------------------

# Slug patterns for markets that settle on a short-duration event.
_SPORTS_SLUG_RE = re.compile(
    r"(?:"
    r"vs[\.\-_ ]"        # Team vs. Team
    r"|nhl|nba|nfl|mlb|ufc|epl|wnba|ncaa"
    r"|cs2|cs-?go|dota|valorant|lol[\-:]"
    r"|bo[135]"          # Best-of-N esports
    r"|set[1-5]|q[1-4]|map-?[1-5]"
    r")",
    re.IGNORECASE,
)


def looks_like_sports_market(slug: str, question: str = "") -> bool:
    """True if the slug/question matches short-duration sports patterns."""
    text = f"{slug} {question}".lower()
    return bool(_SPORTS_SLUG_RE.search(text))


def hours_to_end(end_date: Optional[str]) -> Optional[float]:
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        return (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return None


def is_live_sports_event(
    *,
    slug: str,
    question: str,
    end_date: Optional[str],
    yes_ask: Optional[float],
    yes_bid: Optional[float],
    no_ask: Optional[float] = None,
    live_window_hours: float = 6.0,
) -> tuple[bool, str]:
    """Heuristically detect an in-progress sports event.

    A market is considered "live" (and therefore unsafe to BUY into) when
    ALL of the following hold:

      * It matches a sports-shaped slug.
      * It resolves within ``live_window_hours`` from now.
      * The orderbook spread is wide (> 5¢) OR one side is missing —
        which is typical during live play as market-makers pull quotes.

    Returns (is_live, reason).
    """
    if not looks_like_sports_market(slug, question):
        return False, ""

    h = hours_to_end(end_date)
    if h is None or h > live_window_hours:
        return False, ""

    # Missing orderbook data → treat as live (conservative).
    if yes_ask is None or yes_bid is None:
        return True, f"sports-no-quote (ends in {h:.1f}h)"

    spread = yes_ask - yes_bid
    if spread > 0.05:
        return True, f"sports-wide-spread {spread:.3f} (ends in {h:.1f}h)"

    # NO side missing while we're looking at YES = asymmetric book, also dangerous
    if no_ask is None:
        return True, f"sports-asymmetric-book (ends in {h:.1f}h)"

    # Crash pattern: one side very close to 0 (< 2¢) → game already
    # effectively decided, any BUY is a "lottery at the bottom".
    if yes_ask < 0.02 or no_ask < 0.02:
        return True, f"sports-decided yes={yes_ask:.3f} no={no_ask:.3f}"

    return False, ""


# ---------------------------------------------------------------------------
# 2. Panic-sell detection for "smart" wallets
# ---------------------------------------------------------------------------

def is_wallet_panic_selling(
    trades: Iterable[dict[str, Any]],
    *,
    lookback_seconds: int = 3600,
    min_sells: int = 3,
    price_drop_pct: float = 0.30,
) -> tuple[bool, str]:
    """True if the wallet's recent activity looks like a liquidation cascade.

    Criteria (within ``lookback_seconds`` of the most recent trade):
      * ``min_sells`` or more SELL trades
      * and the average SELL price is at least ``price_drop_pct`` below
        the max recent BUY price on any asset (i.e. the wallet is
        exiting underwater)

    This is exactly the Counter-Strike scenario: the proxy wallet had
    bought at 30-60¢ and was dumping at 0.5-5¢ because the match had
    already turned — clearly not alpha to be copied.
    """
    trades = list(trades or [])
    if not trades:
        return False, ""

    # Find the timestamp of the most recent trade
    try:
        latest_ts = max(int(t.get("timestamp") or 0) for t in trades)
    except ValueError:
        return False, ""
    cutoff = latest_ts - lookback_seconds

    recent = [t for t in trades if int(t.get("timestamp") or 0) >= cutoff]
    sells = [t for t in recent if (t.get("side") or "").upper() == "SELL"]
    buys = [t for t in recent if (t.get("side") or "").upper() == "BUY"]

    if len(sells) < min_sells:
        return False, ""

    # Group by asset; for each asset where we have both sides in the lookback,
    # compare sell prices vs buy prices. If the wallet is dumping at a
    # significant discount vs their own prior buys -> panic.
    buys_by_asset: dict[str, list[float]] = {}
    for t in buys:
        a = str(t.get("asset") or "")
        try:
            buys_by_asset.setdefault(a, []).append(float(t.get("price") or 0))
        except (TypeError, ValueError):
            continue

    for t in sells:
        a = str(t.get("asset") or "")
        try:
            sell_px = float(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        past_buys = buys_by_asset.get(a, [])
        if not past_buys:
            continue
        best_entry = max(past_buys)
        if best_entry <= 0:
            continue
        drawdown = (best_entry - sell_px) / best_entry
        if drawdown >= price_drop_pct:
            return True, (
                f"panic-dump asset={a[:10]}... "
                f"entry_peak={best_entry:.3f} sell={sell_px:.3f} "
                f"dd={drawdown:.1%} recent_sells={len(sells)}"
            )

    # Alternate signal: many sells, few or no buys, prices trending down
    if not buys and len(sells) >= min_sells:
        sell_prices = []
        for t in sells:
            try:
                sell_prices.append(float(t.get("price") or 0))
            except (TypeError, ValueError):
                continue
        if len(sell_prices) >= min_sells:
            first = sell_prices[-1]
            last = sell_prices[0]
            if first > 0 and (first - last) / first >= price_drop_pct / 2:
                return True, (
                    f"sell-only-streak {len(sells)} sells, "
                    f"first_px={first:.3f} last_px={last:.3f}"
                )

    # Concentration: >80% of recent trades are sells of the same asset
    if sells:
        asset_counts = Counter(str(t.get("asset") or "") for t in sells)
        most_common_asset, n = asset_counts.most_common(1)[0]
        if n >= min_sells and n / max(len(recent), 1) >= 0.80:
            return True, (
                f"concentrated-liquidation asset={most_common_asset[:10]}... "
                f"{n}/{len(recent)} trades"
            )

    return False, ""


# ---------------------------------------------------------------------------
# 3. Orderbook liquidity gate
# ---------------------------------------------------------------------------

def orderbook_has_liquidity(
    book: Optional[dict[str, Any]],
    *,
    desired_size_usdc: float,
    side: str = "BUY",
    max_spread: float = 0.05,
    min_depth_multiplier: float = 1.5,
) -> tuple[bool, str]:
    """Check there is enough depth and a tight-enough spread to enter safely.

    * ``max_spread`` rejects illiquid books where the exit slippage would
      eat any possible edge.
    * ``min_depth_multiplier`` ensures the opposite side has enough
      resting orders to absorb our own exit — if we want to buy $2, we
      want at least $3 of bids so we can sell later.

    Polymarket's ``/book`` endpoint returns bid/ask levels in a layout
    where ``[0]`` is the *deepest* (worst) quote and the best price sits
    at ``[-1]``. We therefore scan all levels to find true best bid (max
    price on the bid side) and best ask (min price on the ask side).
    """
    if not book:
        return False, "no-book"

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return False, "empty-book"

    # Extract best bid (highest bid price) and best ask (lowest ask price)
    # without assuming the ordering of the returned list.
    try:
        best_bid = max(float(b["price"]) for b in bids)
        best_ask = min(float(a["price"]) for a in asks)
    except Exception:  # noqa: BLE001
        return False, "malformed-book"

    spread = best_ask - best_bid
    if spread > max_spread:
        return False, f"spread-too-wide {spread:.3f}"

    # Depth on the side we'll need to exit on: for BUY we eventually SELL
    # into bids; for SELL we BUY into asks. Sum the dollar value of the
    # top-5 levels (regardless of return order).
    exit_side_raw = bids if side.upper() == "BUY" else asks
    try:
        sorted_levels = sorted(
            exit_side_raw,
            key=lambda lv: float(lv.get("price", 0)),
            reverse=(side.upper() == "BUY"),  # BUY -> we'll SELL into highest bids first
        )[:5]
        depth_usdc = sum(
            float(lv.get("price", 0)) * float(lv.get("size", 0))
            for lv in sorted_levels
        )
    except Exception:  # noqa: BLE001
        return False, "malformed-book"

    required = desired_size_usdc * min_depth_multiplier
    if depth_usdc < required:
        return False, f"depth-too-thin ${depth_usdc:.2f} < ${required:.2f}"

    return True, f"ok spread={spread:.3f} exit_depth=${depth_usdc:.2f}"


# ---------------------------------------------------------------------------
# 4. Risk-adjusted edge filter
# ---------------------------------------------------------------------------

def risk_adjusted_edge_ok(
    *,
    entry_price: float,
    stop_loss_price: float,
    min_ratio: float = 1.0,
) -> tuple[bool, str]:
    """Reject trades whose upside / downside ratio is below ``min_ratio``.

    Example: buying YES at 0.92 with a stop at 0.72 means
        upside   = 1.00 - 0.92 = 0.08  (if it resolves)
        downside = 0.92 - 0.72 = 0.20  (if SL fires)
        ratio    = 0.08 / 0.20 = 0.4  →  reject.

    To be accepted at ``min_ratio=1.0`` we need a tighter SL or a lower
    entry: e.g. buy at 0.90 with SL at 0.82 → 0.10 / 0.08 = 1.25 ✓
    """
    if entry_price <= 0:
        return False, "entry<=0"
    upside = max(0.0, 1.0 - entry_price)
    downside = max(0.0, entry_price - stop_loss_price)
    if downside <= 1e-6:
        return True, f"downside~0 (upside={upside:.3f})"
    ratio = upside / downside
    if ratio < min_ratio:
        return False, (
            f"ratio={ratio:.2f} < {min_ratio:.2f} "
            f"(up={upside:.3f} / dn={downside:.3f})"
        )
    return True, f"ratio={ratio:.2f} (up={upside:.3f} / dn={downside:.3f})"


# ---------------------------------------------------------------------------
# 5. Max-hold timer
# ---------------------------------------------------------------------------

def position_should_force_exit(
    *,
    opened_at: Optional[float],
    max_hold_seconds: float,
    now: Optional[float] = None,
) -> bool:
    """True if a position has been open longer than ``max_hold_seconds``."""
    if not opened_at or max_hold_seconds <= 0:
        return False
    now = now or time.time()
    return (now - opened_at) >= max_hold_seconds
