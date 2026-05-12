"""Tests for ArbitrageStrategy — it must only emit when YES+NO < 1-min_profit."""

from __future__ import annotations

import pytest

from bot.clients.polymarket import PolyMarket
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.arbitrage import ArbitrageStrategy


def _enriched(yes_ask: float, no_ask: float, volume: float = 5000.0) -> EnrichedMarket:
    m = PolyMarket(
        condition_id="0xCOND",
        slug="test-market",
        question="test",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        volume_usdc=volume,
    )
    em = EnrichedMarket(market=m)
    em.yes_ask = yes_ask
    em.no_ask = no_ask
    em.yes_bid = yes_ask - 0.01
    em.no_bid = no_ask - 0.01
    return em


@pytest.mark.asyncio
async def test_emits_opportunity_on_clear_edge():
    strat = ArbitrageStrategy()
    em = _enriched(yes_ask=0.48, no_ask=0.50)
    snap = MarketSnapshot(arbitrage_candidates=[em], markets={em.market.condition_id: em})
    opps = list(await strat.generate(snap))
    assert len(opps) == 1
    opp = opps[0]
    assert opp.strategy == "arbitrage"
    assert len(opp.legs) == 2
    assert opp.reference_price == pytest.approx(0.98)
    assert opp.expected_profit_pct == pytest.approx(0.02, abs=1e-6)
    assert opp.priority == 0


@pytest.mark.asyncio
async def test_rejects_when_below_min_profit():
    strat = ArbitrageStrategy()
    # Only 0.5% edge — below default 1% ARB_MIN_PROFIT
    em = _enriched(yes_ask=0.50, no_ask=0.495)
    # Scanner would not have bucketed this, but test defensively in the strategy too:
    em2 = _enriched(yes_ask=0.50, no_ask=0.485)  # 1.5% edge — accepted
    snap = MarketSnapshot(arbitrage_candidates=[em, em2])
    opps = list(await strat.generate(snap))
    # The strategy re-checks itself, so the below-threshold one must be dropped.
    assert len(opps) == 1
    assert opps[0].metadata["total_ask"] < 0.99


@pytest.mark.asyncio
async def test_legs_split_notional_evenly():
    strat = ArbitrageStrategy()
    em = _enriched(yes_ask=0.30, no_ask=0.60)
    snap = MarketSnapshot(arbitrage_candidates=[em])
    opps = list(await strat.generate(snap))
    assert len(opps) == 1
    total_size = sum(l.size_usdc for l in opps[0].legs)
    assert opps[0].legs[0].size_usdc == pytest.approx(total_size / 2)
    assert opps[0].legs[1].size_usdc == pytest.approx(total_size / 2)


@pytest.mark.asyncio
async def test_skips_when_prices_missing():
    strat = ArbitrageStrategy()
    em = _enriched(yes_ask=0.4, no_ask=0.5)
    em.no_ask = None  # incomplete
    snap = MarketSnapshot(arbitrage_candidates=[em])
    opps = list(await strat.generate(snap))
    assert opps == []
