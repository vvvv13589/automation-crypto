"""Configuration loading: YAML file merged over built-in defaults."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "mode": "paper",
    "market": "future",  # future = USDT perpetual (long/short, leverage) | spot (long only)
    "exchange": {"id": "binance", "sandbox": False, "demo": False},
    "symbol": "ETH/USDT:USDT",  # perpetual; use "ETH/USDT" for spot
    "timeframe": "4h",
    "history_bars": 500,
    "poll_seconds": 5,
    "state_dir": "state",
    "log_dir": "logs",
    "futures": {
        "leverage": 5,
        "margin_mode": "isolated",
        "exchange_stop": True,  # protective stop order resting on the exchange
        "funding_rate": 0.0001,  # backtest/paper estimate per 8h, charged either side
    },
    "orders": {
        "entry_type": "market",  # market | limit (post-only, maker fee; breakouts often run away)
        "limit_offset_bps": 0,  # place limit this many 0.01% better than the signal close
        "limit_ttl_bars": 1,  # cancel an unfilled entry after this many candles
    },
    "paper": {"starting_cash": 300.0, "maker_fee": 0.0002, "taker_fee": 0.0005, "slippage": 0.0003},
    "daytrade": {
        "enabled": False,
        "timezone": "Asia/Taipei",
        "session_end": "07:45",  # everything flat at this time; next trading day starts
        "no_entry_minutes_before_end": 60,
        "max_trades_per_day": 6,
    },
    "notify": {"telegram": True},
    "scanner": {
        "enabled": True,  # scan the whole market (paper/live); --symbol trades one coin instead
        "coins": "tested",  # tested = only the 55 backtested crypto coins | "all" | ["BTC", "ETH", ...]
        "top_n": 50,  # universe = top N of those by 24h quote volume
        "min_quote_volume": 20_000_000,  # skip thin markets (24h volume in USDT)
        "refresh_hours": 24,  # re-pick the universe this often
        "max_positions": 4,  # open positions at the same time, one per coin
        "risk_per_trade": 0.0075,  # per position; 4 x 0.75%: worst 2024-26 drop 16.5% (< 20% breaker)
        "settle_seconds": 20,  # wait after a candle closes before fetching it
        "exclude": [],  # base assets to never trade, e.g. ["PEPE"]
    },  # needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env
    "goals": {
        "base_capital": 0,  # your stake in USDT; 0 = no milestone alerts
        "double_alert": True,  # Telegram when the account doubles: take the stake out
        "target": 0,  # stop opening new positions once the account reaches this (0 = off)
        "stop_at_target": True,
    },
    "strategy": {
        "name": "breakout",  # breakout (4h Donchian) | pullback | adaptive
        "allow_long": True,
        "allow_short": True,
        "ema_fast": 12,
        "ema_slow": 26,
        "ema_trend": 200,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "bb_period": 20,
        "bb_std": 2.0,
        "atr_period": 14,
        "adx_period": 14,
        "adx_trend": 25,
        "adx_range": 20,
        "stop_atr_mult": 3.0,
        "trail_atr_mult": 4.0,
        "adx_rising": True,
        "adx_rising_bars": 3,
        "trend_exit_on_di": False,
        "range_trend_filter": True,
        # --- breakout strategy ---
        "bo_n": 40,
        "bo_min_volume_ratio": 0,  # >0: require breakout volume >= N x average (0 = rank only)
        "bo_volume_lookback": 180,
        # --- pullback strategy ---
        "htf_timeframe": "4h",
        "htf_ema_fast": 20,
        "htf_ema_slow": 50,
        "pullback_ema": 20,
        "pullback_rsi": 40,
        "pullback_lookback": 8,
        "min_stop_atr": 1.0,
        "max_stop_atr": 3.0,
        "take_profit_r": 2.0,
        "breakeven_r": 1.0,
    },
    "risk": {
        "risk_per_trade": 0.015,
        "max_exposure": 5.0,  # max position notional as a multiple of equity
        "max_drawdown": 0.20,
        "daily_loss_limit": 0.06,
        "min_notional": 20.0,
        "cooldown_bars": 0,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Load config from ``path`` (or ./config.yaml if present) over DEFAULTS."""
    if path is None and Path("config.yaml").exists():
        path = "config.yaml"
    user = {}
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            user = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, user)
    if cfg["market"] == "spot":
        cfg["strategy"]["allow_short"] = False
    return cfg


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so API keys never have to live in config.yaml."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
