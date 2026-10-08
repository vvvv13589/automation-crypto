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
    default_type = "swap" if cfg.get("market") == "future" else "spot"
    # fetchCurrencies hits the spot wallet API (sapi), which futures-only keys may not allow
    params = {"enableRateLimit": True,
              "options": {"defaultType": default_type, "fetchCurrencies": False}}
    if api_key:
        params.update(apiKey=api_key, secret=secret)
        if password:
            params["password"] = password
    exchange = klass(params)
    if ex_cfg.get("demo"):
        exchange.enable_demo_trading(True)  # Binance demo trading (replaces futures testnet)
    elif ex_cfg.get("sandbox"):
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


VISION_URL = "https://data.binance.vision/data/futures/um"


def _vision_symbol(symbol: str) -> str:
    """'ETH/USDT:USDT' -> 'ETHUSDT'."""
    return symbol.split(":")[0].replace("/", "")


def _read_vision_zip(blob: bytes) -> pd.DataFrame:
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        with zf.open(zf.namelist()[0]) as fh:
            raw = pd.read_csv(fh, header=None, usecols=range(6))
    raw = raw[pd.to_numeric(raw[0], errors="coerce").notna()]  # drop header row if present
    raw.columns = COLUMNS
    return to_frame(raw.astype(float).astype({"timestamp": "int64"}).values.tolist())


def fetch_binance_vision(symbol: str, timeframe: str, days: int, cache_dir: str = "data/vision") -> pd.DataFrame:
    """USDT-M futures candles from Binance's public archive (data.binance.vision).

    Monthly files for complete months, daily files for the current month.
    Works where the trading API is geo-blocked; downloads are cached.
    """
    import urllib.error
    import urllib.request
    from pathlib import Path

    sym = _vision_symbol(symbol)
    end = pd.Timestamp.now(tz="UTC").normalize()
    start = end - pd.Timedelta(days=days)
    cache = Path(cache_dir) / sym / timeframe
    cache.mkdir(parents=True, exist_ok=True)

    names = []
    month, this_month = start.tz_localize(None).to_period("M"), end.tz_localize(None).to_period("M")
    while month < this_month:
        names.append(("monthly", f"{sym}-{timeframe}-{month}"))
        month += 1
    day = max(start, this_month.start_time.tz_localize("UTC"))
    while day < end:
        names.append(("daily", f"{sym}-{timeframe}-{day:%Y-%m-%d}"))
        day += pd.Timedelta(days=1)

    frames = []
    for kind, name in names:
        path = cache / f"{name}.zip"
        if not path.exists():
            url = f"{VISION_URL}/{kind}/klines/{sym}/{timeframe}/{name}.zip"
            try:
                with urllib.request.urlopen(url, timeout=60) as resp:
                    path.write_bytes(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:  # not published yet
                    continue
                raise
        frames.append(_read_vision_zip(path.read_bytes()))
    if not frames:
        raise RuntimeError(f"no archive data for {sym} {timeframe}")
    df = pd.concat(frames).sort_index()
    df = df[~df.index.duplicated()]
    return df[df.index >= start]
