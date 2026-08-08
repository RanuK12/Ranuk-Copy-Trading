#!/usr/bin/env python3
"""
Historical data fetcher for Polymarket backtests.
Fetches real market data from Polymarket's Gamma API.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests
from bot.clients.polymarket import PolyMarket
from bot.scanner import EnrichedMarket, MarketSnapshot
from bot.backtest.engine import MarketFrame

logger = logging.getLogger(__name__)

class HistoricalDataFetcher:
    """
    Fetches historical market data from Polymarket's Gamma API
    """
    
    def __init__(self):
        self.base_url = "https://gamma-api.polymarket.com"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Ranuk-Backtest/1.0",
            "Accept": "application/json"
        })
        
    def get_markets(self, start_date: str, end_date: str, limit: int = 100) -> List[Dict]:
        """
        Get markets from the Gamma API within a date range
        """
        try:
            # Convert dates to Unix timestamps
            start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
            end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp())
            
            params = {
                "start": start_ts,
                "end": end_ts,
                "limit": limit,
                "type": "binary",
                "active": "true"
            }
            
            response = self.session.get(
                f"{self.base_url}/markets",
                params=params,
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            return data if isinstance(data, list) else data.get("markets", [])
            
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            return []
    
    def get_market_history(self, market_id: str, days: int = 30) -> List[Dict]:
        """
        Get historical price data for a specific market
        """
        try:
            end_time = int(time.time())
            start_time = end_time - (days * 24 * 60 * 60)
            
            params = {
                "start": start_time,
                "end": end_time,
                "resolution": "1d"  # Daily resolution
            }
            
            response = self.session.get(
                f"{self.base_url}/markets/{market_id}/history",
                params=params,
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            return data if isinstance(data, list) else data.get("history", [])
            
        except Exception as e:
            logger.error(f"Error fetching market history for {market_id}: {e}")
            return []
    
    def create_market_from_api_data(self, market_data: Dict) -> Optional[PolyMarket]:
        """
        Create a PolyMarket object from API data
        """
        try:
            return PolyMarket(
                condition_id=market_data.get("id"),
                slug=market_data.get("slug"),
                question=market_data.get("question"),
                yes_token_id=market_data.get("yesToken", {}).get("id"),
                no_token_id=market_data.get("noToken", {}).get("id"),
                volume_usdc=market_data.get("volume", 0),
                created_at=datetime.fromtimestamp(market_data.get("created", 0)),
                outcome_prices={
                    "yes": market_data.get("yesPrice", 0.5),
                    "no": market_data.get("noPrice", 0.5)
                }
            )
        except Exception as e:
            logger.error(f"Error creating market from API data: {e}")
            return None
    
    def load_historical_data(self, start_date: str, end_date: str, max_markets: int = 50) -> List[MarketFrame]:
        """
        Load historical market data from Polymarket API
        """
        logger.info(f"Loading historical data from {start_date} to {end_date}")
        
        # Parse dates
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        
        # Get markets
        markets_data = self.get_markets(start_date, end_date, max_markets)
        logger.info(f"Found {len(markets_data)} markets")
        
        frames = []
        current_date = start
        
        while current_date <= end:
            day_frames = []
            
            for market_data in markets_data:
                # Create market object
                market = self.create_market_from_api_data(market_data)
                if not market:
                    continue
                
                # Get historical data for this market
                history = self.get_market_history(market.condition_id, 1)
                
                if not history:
                    # Use current market data if no history available
                    yes_ask = market.outcome_prices.get("yes", 0.5)
                    no_ask = market.outcome_prices.get("no", 0.5)
                else:
                    # Use historical data
                    latest = history[-1]
                    yes_ask = latest.get("yesPrice", 0.5)
                    no_ask = latest.get("noPrice", 0.5)
                
                # Create enriched market
                em = EnrichedMarket(
                    market=market,
                    yes_ask=yes_ask,
                    no_ask=no_ask
                )
                
                # Create snapshot
                snap = MarketSnapshot(
                    arbitrage_candidates=[em],
                    markets={market.condition_id: em}
                )
                
                # Determine outcome (if market is resolved)
                outcome = None
                if market_data.get("resolved"):
                    outcome = 1 if market_data.get("outcome") == "YES" else 0
                
                day_frames.append(MarketFrame(
                    snapshot=snap,
                    outcomes={market.condition_id: outcome}
                ))
            
            if day_frames:
                frames.extend(day_frames)
            
            # Move to next day
            current_date += timedelta(days=1)
            
            # Rate limiting
            time.sleep(0.5)
        
        logger.info(f"Loaded {len(frames)} market frames")
        return frames