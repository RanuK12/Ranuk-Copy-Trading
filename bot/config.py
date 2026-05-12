"""Typed, env-driven configuration.

All runtime configuration is centralized here. Every other module imports
:data:`CFG` from this module rather than reading ``os.environ`` directly,
which makes the config easy to mock in tests and trivially inspectable in
the Rich dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: bool = False) -> bool:
    return _env(name, "true" if default else "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str, default: str = "") -> list[str]:
    raw = _env(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _env_float_list(name: str, default: str) -> list[float]:
    return [float(x) for x in _env_list(name, default)]


Mode = Literal["paper", "live"]


@dataclass(frozen=True)
class Config:
    # ---- Mode / logging ----
    mode: Mode = _env("MODE", "paper").lower()  # type: ignore[assignment]
    log_level: str = _env("LOG_LEVEL", "INFO").upper()
    strategies_enabled: list[str] = field(
        default_factory=lambda: _env_list("STRATEGIES_ENABLED", "arbitrage,tail_end,smart_copy")
    )

    # ---- Polymarket wallet ----
    poly_private_key: str = _env("POLY_PRIVATE_KEY")
    poly_funder: str = _env("POLY_FUNDER")
    poly_api_key: str = _env("POLY_API_KEY")
    poly_signature_type: int = _env_int("POLY_SIGNATURE_TYPE", 1)

    # ---- Polymarket APIs ----
    clob_host: str = _env("CLOB_HOST", "https://clob.polymarket.com")
    data_api_host: str = _env("DATA_API_HOST", "https://data-api.polymarket.com")
    gamma_api_host: str = _env("GAMMA_API_HOST", "https://gamma-api.polymarket.com")
    clob_wss_host: str = _env("CLOB_WSS_HOST", "wss://ws-subscriptions-clob.polymarket.com/ws")

    # ---- RPC ----
    alchemy_wss: str = _env("ALCHEMY_WSS_URL")
    alchemy_http: str = _env("ALCHEMY_HTTP_URL")
    quicknode_wss: str = _env("QUICKNODE_WSS_URL")
    quicknode_http: str = _env("QUICKNODE_HTTP_URL")

    # ---- Supabase ----
    supabase_url: str = _env("SUPABASE_URL")
    supabase_key: str = _env("SUPABASE_KEY")

    # ---- Telegram ----
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")

    # ---- Capital ----
    total_capital_usdc: float = _env_float("TOTAL_CAPITAL_USDC", 1000.0)
    default_trade_size: float = _env_float("DEFAULT_TRADE_SIZE_USDC", 20.0)

    # ---- Risk ----
    max_exposure_per_market: float = _env_float("MAX_EXPOSURE_PER_MARKET", 0.05)
    max_exposure_per_strategy: float = _env_float("MAX_EXPOSURE_PER_STRATEGY", 0.25)
    daily_loss_cap: float = _env_float("DAILY_LOSS_CAP", 0.05)
    monthly_loss_cap: float = _env_float("MONTHLY_LOSS_CAP", 0.15)
    max_drawdown: float = _env_float("MAX_DRAWDOWN", 0.10)
    max_consecutive_losses: int = _env_int("MAX_CONSECUTIVE_LOSSES", 4)
    max_slippage: float = _env_float("MAX_SLIPPAGE", 0.02)
    api_error_pause_threshold: int = _env_int("API_ERROR_PAUSE_THRESHOLD", 3)
    api_error_pause_seconds: int = _env_int("API_ERROR_PAUSE_SECONDS", 300)

    # ---- Strategy tuning ----
    arb_min_profit: float = _env_float("ARB_MIN_PROFIT", 0.01)
    arb_min_volume: float = _env_float("ARB_MIN_VOLUME_USDC", 1000.0)

    tail_end_max_days: int = _env_int("TAIL_END_MAX_DAYS", 7)
    tail_end_min_price: float = _env_float("TAIL_END_MIN_PRICE", 0.93)
    tail_end_stop_loss: float = _env_float("TAIL_END_STOP_LOSS", 0.88)

    micro_price_min: float = _env_float("MICRO_PRICE_MIN", 0.05)
    micro_price_max: float = _env_float("MICRO_PRICE_MAX", 0.10)
    micro_min_spread: float = _env_float("MICRO_MIN_SPREAD", 0.05)
    micro_min_volume_per_min: float = _env_float("MICRO_MIN_VOLUME_PER_MIN", 500.0)
    micro_capital_pct: float = _env_float("MICRO_CAPITAL_PCT", 0.20)

    dip_min_drop: float = _env_float("DIP_MIN_DROP", 0.15)
    dip_lookback_seconds: int = _env_int("DIP_LOOKBACK_SECONDS", 3)

    smart_wallets: list[str] = field(
        default_factory=lambda: [w.lower() for w in _env_list("SMART_WALLETS", "")][:10]
    )
    copy_min_win_rate: float = _env_float("COPY_MIN_WIN_RATE", 0.60)
    copy_min_profit_factor: float = _env_float("COPY_MIN_PROFIT_FACTOR", 1.5)
    copy_min_total_pnl: float = _env_float("COPY_MIN_TOTAL_PNL_USDC", 500.0)
    copy_min_consistency: float = _env_float("COPY_MIN_CONSISTENCY", 0.70)
    copy_max_single_trade_pct: float = _env_float("COPY_MAX_SINGLE_TRADE_PCT", 0.30)

    mm_max_total_price: float = _env_float("MM_MAX_TOTAL_PRICE", 0.98)
    mm_ladder_levels: int = _env_int("MM_LADDER_LEVELS", 3)

    sniper_prices: list[float] = field(
        default_factory=lambda: _env_float_list("SNIPER_PRICES", "0.01,0.02,0.03")
    )
    sniper_weights: list[float] = field(
        default_factory=lambda: _env_float_list("SNIPER_WEIGHTS", "0.5,0.3,0.2")
    )

    # ---- Intervals ----
    scan_interval: float = _env_float("SCAN_INTERVAL_SECONDS", 5.0)
    heartbeat_interval: float = _env_float("HEARTBEAT_INTERVAL_SECONDS", 15.0)
    dashboard_refresh: float = _env_float("DASHBOARD_REFRESH_SECONDS", 1.0)
    poly_rate_limit_per_min: int = _env_int("POLY_RATE_LIMIT_PER_MIN", 60)

    # ---- Persistence ----
    state_file: Path = field(default_factory=lambda: Path(_env("STATE_FILE", "./bot_state.json")))

    # ---- Derived helpers ----
    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def max_market_usdc(self) -> float:
        return self.total_capital_usdc * self.max_exposure_per_market

    @property
    def max_strategy_usdc(self) -> float:
        return self.total_capital_usdc * self.max_exposure_per_strategy

    @property
    def daily_loss_usdc(self) -> float:
        return self.total_capital_usdc * self.daily_loss_cap

    @property
    def monthly_loss_usdc(self) -> float:
        return self.total_capital_usdc * self.monthly_loss_cap

    @property
    def max_drawdown_usdc(self) -> float:
        return self.total_capital_usdc * self.max_drawdown


CFG = Config()
