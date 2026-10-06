"""Market data: exchange OHLCV via ccxt, CSV I/O and synthetic data for offline use."""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def make_exchange(cfg: dict, api_key: str | None = None, secret: str | None = None,
                  password: str | None = None):
    import ccxt

    ex_cfg = cfg["exchange"]
    klass = getattr(ccxt, ex_cfg["id"])
    params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
    if api_key:
        params.update(apiKey=api_key, secret=secret)
        if password:
            params["password"] = password
    exchange = klass(params)
    if ex_cfg.get("sandbox"):
        exchange.set_sandbox_mode(True)
    exchange.load_markets()
    return exchange


def to_frame(rows: list[list]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates("timestamp").set_index("timestamp").sort_index()
    return df.astype(float)


def timeframe_ms(timeframe: str) -> int:
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    return int(timeframe[:-1]) * units[timeframe[-1]] * 1000


def fetch_recent(exchange, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """Recent candles with the still-forming last candle removed."""
    df = to_frame(exchange.fetch_ohlcv(symbol, timeframe, limit=limit + 1))
    now_ms = exchange.milliseconds()
    last_open_ms = int(df.index[-1].timestamp() * 1000)
    if last_open_ms + timeframe_ms(timeframe) > now_ms:
        df = df.iloc[:-1]
    return df


def fetch_history(exchange, symbol: str, timeframe: str, since_ms: int,
                  until_ms: int | None = None) -> pd.DataFrame:
    """Paginated historical download."""
    until_ms = until_ms or exchange.milliseconds()
    step = timeframe_ms(timeframe)
    rows: list[list] = []
    cursor = since_ms
    while cursor < until_ms:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        nxt = batch[-1][0] + step
        if nxt <= cursor:
            break
        cursor = nxt
        time.sleep(exchange.rateLimit / 1000)
    df = to_frame(rows)
    return df[df.index < pd.Timestamp(until_ms, unit="ms", tz="UTC")]


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    ts = df["timestamp"]
    unit = "ms" if np.issubdtype(ts.dtype, np.number) else None
    df["timestamp"] = pd.to_datetime(ts, unit=unit, utc=True)
    return df.set_index("timestamp").sort_index()[COLUMNS[1:]].astype(float)


def synthetic(bars: int = 3000, timeframe: str = "15m", start_price: float = 30000.0,
              seed: int = 42) -> pd.DataFrame:
    """Regime-switching random walk (alternating trends and ranges) for offline tests."""
    rng = np.random.default_rng(seed)
    closes = np.empty(bars)
    price, i = start_price, 0
    while i < bars:
        length = int(rng.integers(150, 400))
        kind = rng.choice(["up", "down", "range"], p=[0.35, 0.3, 0.35])
        drift = {"up": 0.0012, "down": -0.0012, "range": 0.0}[kind]
        anchor = price
        for _ in range(min(length, bars - i)):
            pull = -0.03 * (price / anchor - 1) if kind == "range" else 0.0
            price *= np.exp(drift + pull + rng.normal(0, 0.004))
            closes[i] = price
            i += 1
    opens = np.concatenate([[start_price], closes[:-1]])
    spread = np.abs(rng.normal(0, 0.002, bars)) * closes
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    idx = pd.date_range("2024-01-01", periods=bars, freq=pd.Timedelta(milliseconds=timeframe_ms(timeframe)), tz="UTC")
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": rng.uniform(10, 100, bars)},
        index=idx,
    )
