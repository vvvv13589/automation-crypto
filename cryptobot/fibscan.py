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


def suggested_leverage(close: float, stop: float, cap: int = 5) -> int:
    """At most `cap`, and low enough that isolated liquidation lies beyond the stop
    (stop distance + 1.5% maintenance/buffer must stay under 1 / leverage)."""
    dist = abs(stop - close) / close
    return max(1, min(cap, int(1 / (dist + 0.015))))


MAX_SIGNALS = 3  # same-direction setups move together: never suggest more than this per day


def qty_fmt(q: float) -> str:
    """Round a quantity DOWN to 3 significant digits (never suggest more than the risk allows)."""
    if q <= 0:
        return "0"
    digits = 2 - int(np.floor(np.log10(q)))
    step = 10.0 ** -digits
    return f"{np.floor(q / step + 1e-9) * step:.{max(digits, 0)}f}"


def signal_message(n: int, total: int, s: FibSetup, equity: float | None, risk: float) -> str:
    """One self-contained Telegram message: why, then numbered Binance-app steps with the exact values."""
    name = s.symbol.split("/")[0]
    r = s.stop - s.close
    lev = suggested_leverage(s.close, s.stop)
    lines = [f"📉 訊號 {n}／{total}：{name} 做空",
             f"第 {s.touches} 次碰到 {s.level:g} 回撤線（{fmt(s.price)}）被壓回",
             f"昨天收盤：{fmt(s.close)}",
             "",
             "【幣安 App 下單步驟】",
             f"1️⃣ 合約 → U本位 → 搜尋 {name}USDT",
             f"2️⃣ 保證金選「逐倉」，槓桿調成 {lev}x",
             "3️⃣ 下單方式選「市價」"]
    if equity and r > 0:
        qty = equity * risk / r
        notional = qty * s.close
        lines += [f"4️⃣ 數量輸入：{qty_fmt(qty)}（單位選 {name}）",
                  f"     ＝ 約 {notional:.0f} U，保證金約 {notional / lev:.1f} U",
                  "     （App 說數量格式不對的話，改成稍微小一點的數字就好，不要往大填）"]
    else:
        lines += [f"4️⃣ 數量 = 願意虧的 U ÷ {fmt(r)}"]
    lines += ["5️⃣ 勾選「止盈／止損」，觸發選「標記價格」",
              f"     止盈：{fmt(s.take_profit)}",
              f"     止損：{fmt(s.stop)}",
              "6️⃣ 按紅色的「賣出／做空」",
              f"7️⃣ 到「持倉」確認有看到止損 {fmt(s.stop)}",
              "",
              "⚠️ 下單前先看現價：",
              f"・低於 {fmt(s.close * 0.97)} → 已經跌太多，這筆放棄",
              f"・高於 {fmt(s.price)} → 已經站回線上，這筆放棄"]
    if equity and r > 0:
        lines.append(f"碰到停損最多虧約 {equity * risk:.1f} U（帳戶 {risk * 100:g}%）")
    return "\n".join(lines)


def build_messages(setups: list[FibSetup], equity: float | None = None, risk: float = 0.015,
                   date: str = "") -> list[str]:
    """Separate Telegram messages: a summary, one per signal (strongest first, at most
    MAX_SIGNALS), and the watchlist."""
    signals = sorted((s for s in setups if s.signal), key=lambda s: (-s.touches, s.level))
    watch = sorted((s for s in setups if s.near), key=lambda s: s.price / s.close)
    picked = signals[:MAX_SIGNALS]
    if picked:
        names = "、".join(s.symbol.split("/")[0] for s in picked)
        extra = f"（共 {len(signals)} 個，只列出最強的 {len(picked)} 個）" if len(signals) > len(picked) else ""
        head = [f"📐 今日回撤掃描 {date}", f"✅ 有 {len(picked)} 個做空訊號：{names}{extra}",
                "同方向的單會一起漲跌，一天最多做 2–3 筆，照算好的數量下，不要加大。",
                "不做也完全沒關係。"]
    else:
        head = [f"📐 今日回撤掃描 {date}", "今天沒有符合條件的訊號，不用下單 🙂"]
    msgs = ["\n".join(head)]
    msgs += [signal_message(i, len(picked), s, equity, risk) for i, s in enumerate(picked, 1)]
    if watch:
        lines = ["👀 觀察中（還沒被壓回，先不要下單）"]
        for s in watch[:6]:
            lines.append(f"・{s.symbol.split('/')[0]}：回撤線 {fmt(s.price)}，現在 {fmt(s.close)}"
                         f"（差 {(s.price / s.close - 1) * 100:.1f}%）")
        msgs.append("\n".join(lines))
    return msgs


def build_message(setups: list[FibSetup], equity: float | None = None, risk: float = 0.015,
                  date: str = "") -> str:
    """All messages joined (for printing)."""
    return "\n\n".join(build_messages(setups, equity, risk, date))


def scan(exchange, symbols: list[str], fetch) -> list[FibSetup]:
    """``fetch(exchange, symbol, '1d', bars)`` returns closed daily candles."""
    out = []
    for sym in symbols:
        try:
            out += analyze(fetch(exchange, sym, "1d", 400), sym)
        except Exception:
            continue  # one bad market must not stop the scan
    return out
