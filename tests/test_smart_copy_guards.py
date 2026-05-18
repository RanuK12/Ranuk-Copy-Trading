"""Tests for SmartCopy guardrails that prevent copying already-lost sports bets.

Covers the night-of-audit fixes:
  * reject copy when market's ``end_date`` is within ``COPY_MIN_HOURS_TO_END``
  * reject copy when the live ask has drifted > ``COPY_MAX_PRICE_DRIFT``
    from the source trade price (stale feed / crashed market)
  * reject copy when live ask dropped below ``COPY_MIN_ENTRY_PRICE``
    even though the source trade was above it
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from bot.clients.polymarket import PolyMarket
from bot.config import CFG
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.smart_copy import SmartCopyStrategy, WalletScore


@contextmanager
def _cfg(**overrides):
    """Temporarily override frozen CFG fields, restore on exit."""
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
    hours_to_end: float = 48.0,
    slug: str = "test-market",
) -> EnrichedMarket:
    end_date = (
        datetime.now(timezone.utc) + timedelta(hours=hours_to_end)
    ).isoformat()
    m = PolyMarket(
        condition_id="0xCOPY",
        slug=slug,
        question="test",
        yes_token_id="tok-yes",
        no_token_id="tok-no",
        volume_usdc=10_000.0,
        end_date=end_date,
    )
    em = EnrichedMarket(market=m)
    em.yes_ask = yes_ask
    em.no_ask = no_ask
    return em


def _score(passes: bool = True) -> WalletScore:
    return WalletScore(
        win_rate=0.80,
        profit_factor=3.0,
        total_pnl=5000.0,
        consistency=0.75,
        max_single_trade_pct=0.15,
        passes=passes,
        computed_at=time.time(),
    )


def _trade(price: float, token_id: str = "tok-yes", side: str = "BUY") -> dict:
    return {
        "side": side,
        "price": price,
        "size": 10.0,
        "conditionId": "0xCOPY",
        "asset": token_id,
        "slug": "test-market",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "transactionHash": "0xabc",
    }


def test_skip_when_market_ends_soon():
    """Sports market ending in <3h should not be copied (price may crash)."""
    with _cfg(
        copy_min_hours_to_end=3.0,
        copy_max_price_drift=0.50,
        copy_min_entry_price=0.30,
    ):
        strat = SmartCopyStrategy()
        em = _enriched(yes_ask=0.92, no_ask=0.08, hours_to_end=1.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        opp = strat._opp_from_trade(_trade(0.92), snap, "0xwallet", _score())
        assert opp is None, "should reject trades on markets resolving within 3h"


def test_allow_when_market_ends_far_out():
    """Same trade but with plenty of time remaining should pass."""
    with _cfg(
        copy_min_hours_to_end=3.0,
        copy_max_price_drift=0.50,
        copy_min_entry_price=0.30,
    ):
        strat = SmartCopyStrategy()
        em = _enriched(yes_ask=0.92, no_ask=0.08, hours_to_end=24.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        opp = strat._opp_from_trade(_trade(0.92), snap, "0xwallet", _score())
        assert opp is not None
        assert opp.legs[0].limit_price == pytest.approx(
            0.92 * (1 + CFG.max_slippage), abs=1e-4
        )


def test_skip_when_live_price_drifted():
    """Source traded at 0.90 but live ask is 0.40 → refuse to chase."""
    with _cfg(
        copy_min_hours_to_end=1.0,
        copy_max_price_drift=0.20,
        copy_min_entry_price=0.30,
    ):
        strat = SmartCopyStrategy()
        em = _enriched(yes_ask=0.40, no_ask=0.60, hours_to_end=24.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        opp = strat._opp_from_trade(_trade(0.90), snap, "0xwallet", _score())
        assert opp is None, "should reject when live ask drifted > max drift"


def test_skip_when_live_below_entry_floor():
    """Source bought at 0.60 but live has tanked to 0.08 → don't copy."""
    with _cfg(
        copy_min_hours_to_end=1.0,
        copy_max_price_drift=0.95,
        copy_min_entry_price=0.30,
    ):
        strat = SmartCopyStrategy()
        em = _enriched(yes_ask=0.08, no_ask=0.92, hours_to_end=24.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        opp = strat._opp_from_trade(_trade(0.60), snap, "0xwallet", _score())
        assert opp is None, "should refuse when live ask fell below copy floor"


def test_skip_lottery_price_still_enforced():
    """The original lottery-price filter keeps working."""
    with _cfg(copy_min_entry_price=0.50):
        strat = SmartCopyStrategy()
        em = _enriched(yes_ask=0.10, no_ask=0.90)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        opp = strat._opp_from_trade(_trade(0.001), snap, "0xwallet", _score())
        assert opp is None


def test_skip_near_one():
    """Prices ≥ 0.95 have no upside edge even for smart_copy."""
    with _cfg(
        copy_min_entry_price=0.30,
        copy_min_hours_to_end=1.0,
        copy_max_price_drift=0.95,
    ):
        strat = SmartCopyStrategy()
        em = _enriched(yes_ask=0.99, no_ask=0.01, hours_to_end=48.0)
        snap = MarketSnapshot(markets={em.market.condition_id: em})
        opp = strat._opp_from_trade(_trade(0.99), snap, "0xwallet", _score())
        assert opp is None
