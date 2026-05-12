"""Tests for OpportunityQueue — priority order and dedup."""

from __future__ import annotations

import asyncio

import pytest

from bot.models import (
    Leg,
    Opportunity,
    OrderKind,
    PRIORITY_ARBITRAGE,
    PRIORITY_SMART_COPY,
    PRIORITY_TAIL_END,
    Side,
)
from bot.queue import OpportunityQueue


def _opp(strategy: str, market: str, priority: int) -> Opportunity:
    return Opportunity(
        strategy=strategy,
        market_id=market,
        market_slug=market,
        priority=priority,
        expected_profit_pct=0.02,
        legs=[
            Leg(
                token_id=f"tok-{market}",
                side=Side.BUY,
                size_usdc=10.0,
                kind=OrderKind.FOK,
            )
        ],
    )


@pytest.mark.asyncio
async def test_pop_returns_higher_priority_first():
    q = OpportunityQueue()
    await q.push(_opp("smart_copy", "m1", PRIORITY_SMART_COPY))
    await q.push(_opp("arbitrage", "m2", PRIORITY_ARBITRAGE))
    await q.push(_opp("tail_end", "m3", PRIORITY_TAIL_END))

    first = await q.pop(timeout=0.1)
    second = await q.pop(timeout=0.1)
    third = await q.pop(timeout=0.1)

    assert first is not None and first.strategy == "arbitrage"
    assert second is not None and second.strategy == "tail_end"
    assert third is not None and third.strategy == "smart_copy"


@pytest.mark.asyncio
async def test_dedup_rejects_same_strategy_market_pair():
    q = OpportunityQueue()
    ok1 = await q.push(_opp("arbitrage", "mkt-A", PRIORITY_ARBITRAGE))
    ok2 = await q.push(_opp("arbitrage", "mkt-A", PRIORITY_ARBITRAGE))
    assert ok1 is True
    assert ok2 is False
    assert len(q) == 1


@pytest.mark.asyncio
async def test_dedup_does_not_affect_different_strategies():
    q = OpportunityQueue()
    await q.push(_opp("arbitrage", "mkt-A", PRIORITY_ARBITRAGE))
    await q.push(_opp("tail_end", "mkt-A", PRIORITY_TAIL_END))
    assert len(q) == 2


@pytest.mark.asyncio
async def test_pop_timeout_returns_none_when_empty():
    q = OpportunityQueue()
    result = await q.pop(timeout=0.05)
    assert result is None
