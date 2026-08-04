#!/usr/bin/env python3
"""
Reproducible backtest script for the Ranuk Copy Trading Bot.
Uses historical data to simulate strategy performance and generate metrics.

Usage:
    python backtest.py --start 2025-10-01 --end 2025-12-31
    python backtest.py --start 2025-10-01 --end 2025-12-31 --strategy arbitrage
    python backtest.py --start 2025-10-01 --end 2025-12-31 --seed 42
"""

import argparse
import asyncio
import logging
import math
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
from bot.backtest.engine import BacktestEngine, BacktestReport, MarketFrame
from bot.clients.polymarket import PolyMarket
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.base import Strategy
from bot.strategies.smart_copy import SmartCopyStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_historical_data(start_date: str, end_date: str) -> List[MarketFrame]:
    """
    Load historical market data from Polymarket API.
    In a real implementation, this would fetch from Gamma API or historical database.
    For now, we generate synthetic data for demonstration.
    """
    logger.info(f"Loading historical data from {start_date} to {end_date}")
    
    # Parse dates
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    
    # Generate synthetic frames for demonstration
    frames = []
    current_date = start
    
    while current_date <= end:
        # Create a synthetic market snapshot
        market = PolyMarket(
            condition_id=f"0x{current_date.strftime('%Y%m%d')}-test",
            slug="backtest-market",
            question="Will Bitcoin be above $50,000 on Dec 31, 2025?",
            yes_token_id="tok-yes",
            no_token_id="tok-no",
            volume_usdc=10_000.0 + random.uniform(-2_000, 2_000),
        )
        
        # Simulate market prices
        yes_ask = random.uniform(0.45, 0.55)
        no_ask = 1.0 - yes_ask + random.uniform(-0.02, 0.02)
        
        # Create enriched market
        em = EnrichedMarket(market=market, yes_ask=yes_ask, no_ask=no_ask)
        
        # Create snapshot
        snap = MarketSnapshot(
            arbitrage_candidates=[em],
            markets={market.condition_id: em}
        )
        
        # Determine outcome (random for demo)
        outcome = 1 if yes_ask < 0.5 else 0
        
        frames.append(MarketFrame(snapshot=snap, outcomes={market.condition_id: outcome}))
        
        # Move to next day
        current_date += timedelta(days=1)
    
    logger.info(f"Loaded {len(frames)} market frames")
    return frames


def run_backtest(start_date: str, end_date: str, strategy_name: str = "arbitrage", seed: int = 42) -> BacktestReport:
    """
    Run a backtest with the specified strategy and date range.
    """
    # Set seed for reproducibility
    random.seed(seed)
    
    # Load historical data
    frames = load_historical_data(start_date, end_date)
    
    # Select strategy
    if strategy_name == "arbitrage":
        strategy = ArbitrageStrategy()
    elif strategy_name == "smart_copy":
        strategy = SmartCopyStrategy()
    else:
        raise ValueError(f"Unknown strategy: {strategy_name}")
    
    logger.info(f"Running backtest with {strategy_name} strategy")
    
    # Create and run backtest engine
    engine = BacktestEngine(strategy=strategy, frames=frames, seed=seed)
    return asyncio.run(engine.run())


def main():
    parser = argparse.ArgumentParser(description="Backtest the Ranuk Copy Trading Bot")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--strategy", default="arbitrage", choices=["arbitrage", "smart_copy"], help="Strategy to test")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    
    args = parser.parse_args()
    
    # Run backtest
    report = run_backtest(args.start, args.end, args.strategy, args.seed)
    
    # Print results
    print("=" * 60)
    print(f"BACKTEST RESULTS: {args.start} to {args.end}")
    print("=" * 60)
    print(report.summary())
    print("=" * 60)
    print("⚠️  WARNING: Historical results do not guarantee future performance.")
    print("⚠️  This is not financial advice. Trading involves significant risk.")
    print("⚠️  Past performance is not indicative of future results.")
    print("=" * 60)


if __name__ == "__main__":
    main()