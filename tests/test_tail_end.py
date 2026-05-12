"""Tests for TailEndStrategy — days + price + confidence logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot.clients.polymarket import PolyMarket
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.tail_end import TailEndStrategy


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
    strat = TailEndStrategy()
    near = _enriched(yes_ask=0.95, no_ask=0.05, days_until=1)
    far = _enriched(yes_ask=0.95, no_ask=0.05, days_until=6)
    snap_near = MarketSnapshot(tail_end_candidates=[near])
    snap_far = MarketSnapshot(tail_end_candidates=[far])
    near_conf = (await strat.generate(snap_near))[0].confidence  # type: ignore
    far_conf = (await strat.generate(snap_far))[0].confidence  # type: ignore
    assert near_conf > far_conf
