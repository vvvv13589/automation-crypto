"""Hourly price-action alert at daily Fibonacci resistance (alerts only, never trades).

Matches the research test (55 coins, 2020-2026): at a daily 0.382 / 0.5 retracement of a >= 40%
fall that has already rejected price at least once (so this is the 2nd+ touch), a 1h candle pattern:
  push    : green candle whose high reaches F (0.3% below to 1.5% above), volume >= 2x 20-candle avg
  hold    : no close above F x 1.005 and no high above F x 1.015 afterwards
  confirm : within 4 candles, a red candle with volume >= 1.5x avg closing below the push's open
Short at the confirm close, stop 0.25 ATR(1h) above the highest high since the push, take profit 3R.
Research: avg +0.34R per trade (2nd+ touch, TP 3R), positive in 2020-22 and 2024+; the same
pattern WITHOUT the volume rules lost money, so the volume rules are not optional.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import indicators as ind
from .fibscan import LEVELS, MIN_DROP, fmt, order_lines

TP_R = 3.0
NEAR_BELOW, NEAR_ABOVE = 0.05, 0.015  # only coins trading within 5% below .. 1.5% above a level


@dataclass
class PASignal:
    symbol: str
    level: float
    price: float          # the daily retracement level F
    prior: int            # daily rejections at F before today
    push_time: pd.Timestamp
    entry: float
    stop: float
    take_profit: float


def daily_levels(d: pd.DataFrame) -> list[tuple[float, float, int]]:
    """(level, F, prior rejections) for today, from closed daily candles only."""
    if len(d) < 120:
        return []
    h, l, c = d["high"].to_numpy(), d["low"].to_numpy(), d["close"].to_numpy()
    t = len(d)
    w0 = max(0, t - 365)
    i_hi = w0 + int(np.argmax(h[w0:t]))
    if i_hi >= t - 21:
        return []
    i_lo = i_hi + int(np.argmin(l[i_hi:t - 20]))
    hi, lo = float(h[i_hi]), float(l[i_lo])
    if (hi - lo) / hi < MIN_DROP:
        return []
    out = []
    for lvl in LEVELS:
        f = lo + lvl * (hi - lo)
        seg = np.nonzero((h[i_lo + 1:t] >= f * 0.995) & (c[i_lo + 1:t] < f))[0]
        touches = 0 if len(seg) == 0 else 1 + int(np.sum(np.diff(seg) >= 10))
        out.append((lvl, f, touches))
    return out


def detect(h1: pd.DataFrame, f: float) -> dict | None:
    """The pattern completing on the last closed 1h candle of ``h1`` at level ``f``."""
    if len(h1) < 40:
        return None
    o, h, c, v = (h1[x].to_numpy() for x in ("open", "high", "close", "volume"))
    atr = ind.atr(h1, 14).to_numpy()
    vavg = h1["volume"].rolling(20).mean().shift(1).to_numpy()
    j = len(h1) - 1
    if not (c[j] < o[j] and vavg[j] > 0 and v[j] >= 1.5 * vavg[j]):
        return None
    for t in range(j - 1, max(j - 5, 20), -1):
        if not (c[t] > o[t] and f * 0.997 <= h[t] <= f * 1.015 and vavg[t] > 0 and v[t] >= 2 * vavg[t]):
            continue
        ext = float(np.max(h[t:j + 1]))
        if ext > f * 1.015 or np.any(c[t + 1:j + 1] > f * 1.005) or c[j] >= o[t]:
            continue
        # an earlier confirm after this push would already have produced the alert
        if any(c[x] < o[x] and v[x] >= 1.5 * vavg[x] and c[x] < o[t] for x in range(t + 1, j)):
            continue
        stop = ext + 0.25 * float(atr[j])
        entry = float(c[j])
        r = stop - entry
        if r <= 0 or r / entry > 0.1:
            return None
        return {"push_time": h1.index[t], "entry": entry, "stop": stop, "take_profit": entry - TP_R * r}
    return None


def scan(exchange, symbols: list[str], fetch) -> list[PASignal]:
    out = []
    for sym in symbols:
        try:
            levels = [x for x in daily_levels(fetch(exchange, sym, "1d", 400)) if x[2] >= 1]
            if not levels:
                continue
            h1 = fetch(exchange, sym, "1h", 60)
            last = float(h1["close"].iloc[-1])
            for lvl, f, prior in levels:
                if not (f * (1 - NEAR_BELOW) <= last <= f * (1 + NEAR_ABOVE)):
                    continue
                hit = detect(h1, f)
                if hit:
                    out.append(PASignal(sym, lvl, f, prior, hit["push_time"], hit["entry"], hit["stop"],
                                        hit["take_profit"]))
                    break
        except Exception:
            continue  # one bad market must not stop the scan
    return out


def message(s: PASignal, equity: float | None, risk: float, margin: float | None, leverage: int | None) -> str:
    name = s.symbol.split("/")[0]
    r = s.stop - s.entry
    head = [f"⚡ 價格行為訊號：{name} 做空",
            f"日線 {s.level:g} 回撤線 {fmt(s.price)}（之前已被壓回 {s.prior} 次）",
            "1 小時：放量衝上去 → 收不住 → 放量跌下來，剛確認",
            f"進場參考價：{fmt(s.entry)}（停損距離約 {r / s.entry * 100:.1f}%）",
            ""]
    return "\n".join(head + order_lines(name, s.entry, s.stop, s.take_profit, equity, risk, margin, leverage,
                                        s.entry - r, s.price))


def unseen(signals: list[PASignal], state_path: str) -> list[PASignal]:
    """Drop signals already alerted (same coin and push candle); remember the new ones for 3 days."""
    path = Path(state_path)
    seen = {}
    if path.exists():
        try:
            seen = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            seen = {}
    now = pd.Timestamp.now(tz="UTC")
    seen = {k: v for k, v in seen.items() if now - pd.Timestamp(v) < pd.Timedelta(days=3)}
    fresh = []
    for s in signals:
        key = f"{s.symbol}|{pd.Timestamp(s.push_time).isoformat()}"
        if key not in seen:
            fresh.append(s)
            seen[key] = now.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    Path(tmp).write_text(json.dumps(seen, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return fresh
