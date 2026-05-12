"""Tests for the BudgetProfile auto-classifier (low-budget support)."""

from __future__ import annotations

from bot.core.budget import classify, filter_strategies, refresh_profile


def test_micro_budget_20_usd():
    profile = classify(20.0)
    assert profile.tier == "micro"
    # Trade size ~ 25% of capital, clamped to minimum $2.
    assert 2.0 <= profile.trade_size_usdc <= 6.0
    # Multi-leg strategies must be forbidden at this tier.
    assert "arbitrage" in profile.forbidden_strategies
    assert "market_making" in profile.forbidden_strategies
    assert "micro_spread" in profile.forbidden_strategies
    # tail_end is the one recommended strategy.
    assert "tail_end" in profile.recommended_strategies


def test_micro_budget_30_usd():
    profile = classify(30.0)
    assert profile.tier == "micro"
    # With $30 capital, daily loss cap is 10% = $3.
    assert profile.daily_loss_cap_usdc == 3.0
    # Max per-market is 40% = $12, rounded.
    assert profile.max_per_market_usdc >= 8.0


def test_small_budget_100_usd():
    profile = classify(100.0)
    assert profile.tier == "small"
    assert "smart_copy" in profile.recommended_strategies
    assert "sniper" in profile.recommended_strategies
    # Market making still not viable at $100.
    assert "market_making" in profile.forbidden_strategies


def test_standard_budget_1000_usd():
    profile = classify(1000.0)
    assert profile.tier == "standard"
    assert profile.trade_size_usdc == 20.0
    assert profile.forbidden_strategies == []
    # All 7 strategies recommended
    assert len(profile.recommended_strategies) == 7


def test_large_budget_10000_usd():
    profile = classify(10_000.0)
    assert profile.tier == "large"
    # Tighter caps at large tier
    assert profile.max_per_market_usdc <= 400  # 3% of 10k = 300
    # All strategies still allowed
    assert profile.forbidden_strategies == []


def test_filter_strategies_drops_forbidden_for_micro():
    refresh_profile(30.0)
    requested = ["tail_end", "arbitrage", "market_making", "smart_copy"]
    allowed, dropped = filter_strategies(requested)
    assert "tail_end" in allowed
    assert "smart_copy" in allowed
    assert "arbitrage" in dropped
    assert "market_making" in dropped


def test_filter_strategies_allows_all_for_standard():
    refresh_profile(1000.0)
    requested = ["arbitrage", "tail_end", "market_making", "micro_spread"]
    allowed, dropped = filter_strategies(requested)
    assert dropped == []
    assert set(allowed) == set(requested)


def test_profile_describe_contains_tier_and_size():
    profile = classify(25.0)
    text = profile.describe()
    assert "[micro]" in text
    assert "$25.00" in text
    assert "Recommended strategies" in text
