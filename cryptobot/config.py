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
    "timeframe": "15m",
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
        "entry_type": "limit",  # limit (post-only, maker fee) | market
        "limit_offset_bps": 0,  # place limit this many 0.01% better than the signal close
        "limit_ttl_bars": 1,  # cancel an unfilled entry after this many candles
    },
    "paper": {"starting_cash": 300.0, "maker_fee": 0.0002, "taker_fee": 0.0005, "slippage": 0.0003},
    "daytrade": {
        "enabled": True,
        "timezone": "Asia/Taipei",
        "session_end": "07:45",  # everything flat at this time; next trading day starts
        "no_entry_minutes_before_end": 60,
        "max_trades_per_day": 6,
    },
    "notify": {"telegram": True},  # needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in .env
    "strategy": {
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
        "stop_atr_mult": 2.5,
        "trail_atr_mult": 3.5,
        "adx_rising": True,
        "adx_rising_bars": 3,
        "trend_exit_on_di": False,
        "range_trend_filter": True,
    },
    "risk": {
        "risk_per_trade": 0.02,
        "max_exposure": 5.0,  # max position notional as a multiple of equity
        "max_drawdown": 0.20,
        "daily_loss_limit": 0.06,
        "min_notional": 20.0,
        "cooldown_bars": 2,
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
