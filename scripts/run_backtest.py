#!/usr/bin/env python3
"""
Reproducible backtest for DipArb strategy using real Binance historical data.

Fetches klines from Binance public API (no API key required), applies the
same RSI(14) < 30 oversold bounce logic that the live DipArb strategy uses,
and computes standard performance metrics.

Usage:
    python scripts/run_backtest.py --pair BTCUSDT --from 2025-01-01 --to 2025-12-31
    python scripts/run_backtest.py --pair ETHUSDT --from 2025-06-01 --to 2025-06-30 --interval 1h
"""

import argparse
import asyncio
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import pandas as pd


# ──────────────────────────────────────────────────────────────────────
# Binance public klines fetcher (no API key)
# ──────────────────────────────────────────────────────────────────────
BINANCE_BASE = "https://api.binance.com/api/v3/klines"
SYMBOL_MAP = {"btc": "BTCUSDT", "eth": "ETHUSDT", "sol": "SOLUSDT"}


async def fetch_klines(
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
) -> List[List]:
    """Fetch klines from Binance public API with pagination."""
    all_klines = []
    current_start = start_ms

    async with httpx.AsyncClient(timeout=30.0) as client:
        while current_start < end_ms:
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": current_start,
                "endTime": end_ms,
                "limit": limit,
            }
            resp = await client.get(BINANCE_BASE, params=params)
            resp.raise_for_status()
            klines = resp.json()
            if not klines:
                break
            all_klines.extend(klines)
            current_start = klines[-1][6] + 1
            if len(klines) < limit:
                break
    return all_klines


def klines_to_dataframe(klines: List[List]) -> pd.DataFrame:
    """Convert raw klines to pandas DataFrame with proper columns."""
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_volume", "taker_buy_quote_volume", "ignore"
    ])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ──────────────────────────────────────────────────────────────────────
# RSI calculation (same logic as live strategy)
# ──────────────────────────────────────────────────────────────────────
def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI using Wilder's smoothing (same as TradingView/TA-Lib)."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()

    for i in range(period, len(gain)):
        avg_gain.iloc[i] = (avg_gain.iloc[i - 1] * (period - 1) + gain.iloc[i]) / period
        avg_loss.iloc[i] = (avg_loss.iloc[i - 1] * (period - 1) + loss.iloc[i]) / period

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


# ──────────────────────────────────────────────────────────────────────
# DipArb backtest logic (reuses the same signal logic as live)
# ──────────────────────────────────────────────────────────────────────
@dataclass
class Trade:
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    pnl_pct: Optional[float]
    pnl_usdc: Optional[float]
    is_win: Optional[bool]
    rsi_at_entry: float


def run_diparb_backtest(
    df: pd.DataFrame,
    rsi_period: int = 14,
    rsi_threshold: float = 30.0,
    take_profit_pct: float = 0.02,
    stop_loss_pct: float = 0.015,
    max_hold_hours: int = 24,
    initial_capital: float = 1000.0,
    position_size_pct: float = 0.10,
) -> Tuple[List[Trade], dict]:
    """Run DipArb backtest on historical data."""
    df = df.copy()
    df["rsi"] = compute_rsi(df["close"], rsi_period)

    trades: List[Trade] = []
    capital = initial_capital
    equity_curve = [capital]
    in_position = False
    entry_price = None
    entry_time = None
    entry_rsi = None

    for i in range(len(df)):
        row = df.iloc[i]
        current_time = row["open_time"]
        current_price = row["close"]
        current_rsi = row["rsi"]

        if pd.isna(current_rsi):
            equity_curve.append(capital)
            continue

        if not in_position:
            if current_rsi < rsi_threshold:
                in_position = True
                entry_price = current_price
                entry_time = current_time
                entry_rsi = current_rsi
        else:
            hold_hours = (current_time - entry_time).total_seconds() / 3600
            price_change = (current_price - entry_price) / entry_price

            should_exit = False
            if price_change >= take_profit_pct:
                should_exit = True
            elif price_change <= -stop_loss_pct:
                should_exit = True
            elif hold_hours >= max_hold_hours:
                should_exit = True

            if should_exit:
                pnl_pct = price_change
                position_size = capital * position_size_pct
                pnl_usdc = position_size * pnl_pct
                capital += pnl_usdc

                trade = Trade(
                    entry_time=entry_time,
                    entry_price=entry_price,
                    exit_time=current_time,
                    exit_price=current_price,
                    pnl_pct=pnl_pct,
                    pnl_usdc=pnl_usdc,
                    is_win=pnl_pct > 0,
                    rsi_at_entry=entry_rsi,
                )
                trades.append(trade)
                in_position = False
                entry_price = None
                entry_time = None
                entry_rsi = None

        equity_curve.append(capital)

    if in_position and entry_price is not None:
        final_price = df.iloc[-1]["close"]
        final_time = df.iloc[-1]["open_time"]
        pnl_pct = (final_price - entry_price) / entry_price
        position_size = capital * position_size_pct
        pnl_usdc = position_size * pnl_pct
        capital += pnl_usdc

        trade = Trade(
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=final_time,
            exit_price=final_price,
            pnl_pct=pnl_pct,
            pnl_usdc=pnl_usdc,
            is_win=pnl_pct > 0,
            rsi_at_entry=entry_rsi,
        )
        trades.append(trade)

    # ──────────────────────────────────────────────────────────────────────
    # Metrics
    # ──────────────────────────────────────────────────────────────────────
    total_return_pct = (capital - initial_capital) / initial_capital * 100
    total_trades = len(trades)
    winning_trades = [t for t