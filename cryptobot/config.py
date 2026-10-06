"""Configuration loading: YAML file merged over built-in defaults."""

from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "mode": "paper",
    "exchange": {"id": "binance", "sandbox": False},
    "symbol": "BTC/USDT",
    "timeframe": "15m",
    "history_bars": 300,
    "poll_seconds": 10,
    "state_dir": "state",
    "log_dir": "logs",
    "paper": {"starting_cash": 10000.0, "fee_rate": 0.001, "slippage": 0.0005},
    "strategy": {
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
        "stop_atr_mult": 2.0,
        "trail_atr_mult": 3.0,
    },
    "risk": {
        "risk_per_trade": 0.01,
        "max_position_pct": 0.5,
        "max_drawdown": 0.20,
        "daily_loss_limit": 0.05,
        "min_notional": 10.0,
        "cooldown_bars": 3,
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
    return _merge(DEFAULTS, user)


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
