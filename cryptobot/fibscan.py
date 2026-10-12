"""Daily Fibonacci-rejection watchlist (alerts only, never trades).

The rule matches the research test (55 coins, daily, 2020-2026), short side only, the one
variant with an edge: after a >= 40% fall from a high H to a later low L (within the last year,
L at least 20 days old), price rallies to the 0.382 or 0.5 retracement F = L + lvl x (H - L).
A *signal* is a daily candle whose high reaches F (within 0.5%) but closes below it, on the
2nd or later such rejection (touches >= 10 days apart). Plan: short at the close, stop 1 ATR(14)
above F, take profit at 3R. Research: 2nd+ touch at 0.382 avg +0.36R, 3rd+ +0.49R; longs and
resting limit ("ambush") entries lost money, so they are not reported.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind

LEVELS = (0.382, 0.5)
MIN_DROP = 0.40
NEAR = 0.03  # "watch" when the close is within 3% below a level
TP_R = 3.0


@dataclass
class FibSetup:
    symbol: str
    level: float          # 0.382 or 0.5
    price: float          # level price F
    close: float
    high: float
    swing_high: float
    swing_low: float
    touches: int          # rejections at this level so far, including today's
    signal: bool          # today's candle touched F and closed below it, touch >= 2
    near: bool            # close within NEAR below F (watchlist)
    stop: float
    take_profit: float


def analyze(df: pd.DataFrame, symbol: str = "") -> list[FibSetup]:
    """Setups on the last *closed* daily candle of ``df`` (open/high/low/close, oldest first)."""
    if len(df) < 120:
        return []
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    atr = float(ind.atr(df, 14).iloc[-1])
    t = len(df) - 1
    w0 = max(0, t - 365)
    i_hi = w0 + int(np.argmax(h[w0:t]))
    if i_hi >= t - 21:
        return []
    i_lo = i_hi + int(np.argmin(l[i_hi:t - 20]))
    hi, lo = float(h[i_hi]), float(l[i_lo])
    if (hi - lo) / hi < MIN_DROP or atr != atr:
        return []
    out = []
    for lvl in LEVELS:
        f = lo + lvl * (hi - lo)
        touches, last = 0, -999
        for j in range(i_lo + 1, t + 1):  # rejections at F since the low
            if h[j] >= f * 0.995 and c[j] < f:
                if j - last >= 10:
                    touches += 1
                last = j
        today = h[t] >= f * 0.995 and c[t] < f
        near = not today and f * (1 - NEAR) <= c[t] < f
        if not (today or near):
            continue
        stop = f + atr
        out.append(FibSetup(symbol, lvl, f, float(c[t]), float(h[t]), hi, lo,
                            touches if today else touches + 1, today and touches >= 2,
                            near, stop, float(c[t]) - TP_R * (stop - float(c[t]))))
    return out


def fmt(x: float) -> str:
    return f"{x:.6g}"


def build_message(setups: list[FibSetup], equity: float | None = None, risk: float = 0.015,
                  date: str = "") -> str:
    signals = sorted((s for s in setups if s.signal), key=lambda s: -s.touches)
    watch = sorted((s for s in setups if s.near), key=lambda s: s.price / s.close)
    lines = [f"📐 回撤壓力掃描 {date}（只做空・只提醒不下單）"]
    if signals:
        lines.append("✅ 符合條件（第 2 次以上被壓回、日線收盤確認）：")
        for s in signals:
            name = s.symbol.split("/")[0]
            r = s.stop - s.close
            size = ""
            if equity and r > 0:
                qty = equity * risk / r
                size = f"｜{risk * 100:g}% 風險 ≈ {fmt(qty)} 顆（名目 {qty * s.close:.0f} U）"
            lines.append(f"  • {name} {s.level:g} 線 {fmt(s.price)}（第 {s.touches} 次）收 {fmt(s.close)}"
                         f"｜停損 {fmt(s.stop)}｜止盈 3R {fmt(s.take_profit)}{size}")
    else:
        lines.append("今天沒有符合條件的訊號。沒有訊號就不用下單 🙂")
    if watch:
        lines.append("👀 觀察中（收盤在回撤線下方 3% 內，還沒被壓回）：")
        for s in watch[:10]:
            name = s.symbol.split("/")[0]
            lines.append(f"  • {name} {s.level:g} 線 {fmt(s.price)}，收 {fmt(s.close)}"
                         f"（差 {(s.price / s.close - 1) * 100:.1f}%，若被壓回是第 {s.touches} 次）")
    lines.append("規則：等日線收盤確認再進場，不要預掛單；停損 = 回撤線 + 1 ATR。")
    return "\n".join(lines)


def scan(exchange, symbols: list[str], fetch) -> list[FibSetup]:
    """``fetch(exchange, symbol, '1d', bars)`` returns closed daily candles."""
    out = []
    for sym in symbols:
        try:
            out += analyze(fetch(exchange, sym, "1d", 400), sym)
        except Exception:
            continue  # one bad market must not stop the scan
    return out
