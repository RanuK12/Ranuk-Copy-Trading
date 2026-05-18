"""Tests for scanner bucket classification after the tail_end tuning.

Verifies the night-of-audit scanner fix:
  * markets with both asks >= 0.99 no longer leak into tail_end_candidates
    (no edge left, wastes strategy loop time)
  * markets with a leading ask in the [min_price, 0.98] band do qualify
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from bot.clients.polymarket import PolyMarket
from bot.config import CFG
from bot.scanner import EnrichedMarket, MarketScanner, MarketSnapshot


@contextmanager
def _cfg(**overrides):
    prev = {k: getattr(CFG, k) for k in overrides}
    try:
        for k, v in overrides.items():
            object.__setattr__(CFG, k, v)
        yield
    finally:
        for k, v in prev.items():
            object.__setattr__(CFG, k, v)


def _make(
    yes_ask: float,
    no_ask: float,
    days_until: float = 3.0,
    volume: float = 5_000.0,
    condition_id: str = "0xMKT",
) -> EnrichedMarket:
    end_date = (datetime.now(timezone.utc) + timedelta(days=days_until)).isoformat()
    m = PolyMarket(
        condition_id=condition_id,
        slug="sample",
        question="?",
        yes_token_id="yes",
        no_token_id="no",
        volume_usdc=volume,
        end_date=end_date,
    )
    em = EnrichedMarket(market=m)
    em.yes_ask = yes_ask
    em.no_ask = no_ask
    return em


def test_tail_end_skips_markets_both_sides_at_one():
    """Both asks at 0.999 → stale / already-resolved, must NOT be tail candidate."""
    with _cfg(tail_end_min_price=0.80, tail_end_max_days=14):
        s = MarketScanner()
        em = _make(yes_ask=0.999, no_ask=0.999, days_until=0.5)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        s._bucket(snap)
        assert em not in snap.tail_end_candidates


def test_tail_end_accepts_sweet_spot():
    """Leading ask at 0.92 with >0 days left → valid tail_end candidate."""
    with _cfg(tail_end_min_price=0.80, tail_end_max_days=14):
        s = MarketScanner()
        em = _make(yes_ask=0.92, no_ask=0.08, days_until=3.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        s._bucket(snap)
        assert em in snap.tail_end_candidates


def test_tail_end_rejects_below_floor():
    """Leading ask below tail_end_min_price → not a candidate."""
    with _cfg(tail_end_min_price=0.80, tail_end_max_days=14):
        s = MarketScanner()
        em = _make(yes_ask=0.60, no_ask=0.40, days_until=3.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        s._bucket(snap)
        assert em not in snap.tail_end_candidates


def test_tail_end_rejects_expired():
    """End_date in the past → must not be a candidate."""
    with _cfg(tail_end_min_price=0.80, tail_end_max_days=14):
        s = MarketScanner()
        em = _make(yes_ask=0.95, no_ask=0.05, days_until=-1.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        s._bucket(snap)
        assert em not in snap.tail_end_candidates
