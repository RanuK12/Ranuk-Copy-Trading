"""Tests for TailEndStrategy — days + price + confidence logic."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from bot.clients.polymarket import PolyMarket
from bot.config import CFG
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.tail_end import TailEndStrategy


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


def _enriched(
    yes_ask: float,
    no_ask: float,
    days_until: float = 3.0,
    volume: float = 5000.0,
) -> EnrichedMarket:
    end_date = (datetime.now(timezone.utc) + timedelta(days=days_until)).isoformat()
    m = PolyMarket(
        condition_id="0xTAIL",
        slug="test-tail",
        question="test",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        volume_usdc=volume,
        end_date=end_date,
    )
    em = EnrichedMarket(market=m)
    em.yes_ask = yes_ask
    em.no_ask = no_ask
    return em


@pytest.mark.asyncio
async def test_buys_leading_side_when_price_above_threshold():
    # Entry 0.95 with SL 0.88 → up 0.05 / down 0.07 → ratio 0.71.
    # Relax R/R ratio here so we still exercise the leading-side logic.
    with _cfg(tail_end_min_rr_ratio=0.5, tail_end_stop_loss=0.88):
        strat = TailEndStrategy()
        em = _enriched(yes_ask=0.95, no_ask=0.05, days_until=2)
        snap = MarketSnapshot(tail_end_candidates=[em])
        opps = list(await strat.generate(snap))
        assert len(opps) == 1
        opp = opps[0]
        # YES is the leading outcome -> we should buy YES
        assert opp.legs[0].token_id == em.market.yes_token_id
        assert opp.metadata["outcome"] == "YES"
        # Expected profit = 1 - 0.95 == 0.05 (5 cents per dollar)
        assert opp.expected_profit_pct == pytest.approx(0.05, abs=1e-6)
        assert 0.85 <= opp.confidence <= 1.0


@pytest.mark.asyncio
async def test_skips_when_all_sides_below_threshold():
    strat = TailEndStrategy()
    # Neither YES nor NO above 0.93
    em = _enriched(yes_ask=0.60, no_ask=0.40)
    snap = MarketSnapshot(tail_end_candidates=[em])
    opps = list(await strat.generate(snap))
    assert opps == []


@pytest.mark.asyncio
async def test_skips_when_resolved():
    strat = TailEndStrategy()
    em = _enriched(yes_ask=0.97, no_ask=0.03, days_until=-1)
    snap = MarketSnapshot(tail_end_candidates=[em])
    opps = list(await strat.generate(snap))
    assert opps == []


@pytest.mark.asyncio
async def test_skips_when_too_far_out():
    strat = TailEndStrategy()
    em = _enriched(yes_ask=0.97, no_ask=0.03, days_until=30)
    snap = MarketSnapshot(tail_end_candidates=[em])
    opps = list(await strat.generate(snap))
    assert opps == []


@pytest.mark.asyncio
async def test_confidence_higher_closer_to_resolution():
    with _cfg(tail_end_min_rr_ratio=0.5, tail_end_stop_loss=0.88):
        strat = TailEndStrategy()
        near = _enriched(yes_ask=0.95, no_ask=0.05, days_until=1)
        far = _enriched(yes_ask=0.95, no_ask=0.05, days_until=6)
        snap_near = MarketSnapshot(tail_end_candidates=[near])
        snap_far = MarketSnapshot(tail_end_candidates=[far])
        near_conf = (await strat.generate(snap_near))[0].confidence  # type: ignore
        far_conf = (await strat.generate(snap_far))[0].confidence  # type: ignore
        assert near_conf > far_conf


@pytest.mark.asyncio
async def test_rejects_when_risk_reward_ratio_too_low():
    """Entry 0.92 with SL 0.72 → up 0.08 / down 0.20 → ratio 0.40 ⇒ reject."""
    with _cfg(tail_end_min_rr_ratio=1.0, tail_end_stop_loss=0.72):
        strat = TailEndStrategy()
        em = _enriched(yes_ask=0.92, no_ask=0.08, days_until=3)
        snap = MarketSnapshot(tail_end_candidates=[em])
        opps = list(await strat.generate(snap))
        assert opps == []


@pytest.mark.asyncio
async def test_accepts_when_risk_reward_ratio_ok():
    """Entry 0.90 with SL 0.82 → up 0.10 / down 0.08 → ratio 1.25 ⇒ accept."""
    with _cfg(tail_end_min_rr_ratio=1.0, tail_end_stop_loss=0.82):
        strat = TailEndStrategy()
        em = _enriched(yes_ask=0.90, no_ask=0.10, days_until=3)
        snap = MarketSnapshot(tail_end_candidates=[em])
        opps = list(await strat.generate(snap))
        assert len(opps) == 1


@pytest.mark.asyncio
async def test_rejects_live_sports_event():
    """Sports-shaped slug resolving in hours with wide spread → skip."""
    with _cfg(tail_end_min_rr_ratio=0.5, tail_end_stop_loss=0.88):
        strat = TailEndStrategy()
        end_date = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        m = PolyMarket(
            condition_id="0xLIVE",
            slug="counter-strike-liquid-vs-m80-bo3",
            question="Will Liquid win?",
            yes_token_id="tok-yes",
            no_token_id="tok-no",
            volume_usdc=5000.0,
            end_date=end_date,
        )
        em = EnrichedMarket(market=m)
        em.yes_ask = 0.95
        em.yes_bid = 0.20  # in-game wide spread
        em.no_ask = 0.80
        snap = MarketSnapshot(tail_end_candidates=[em])
        opps = list(await strat.generate(snap))
        assert opps == []
