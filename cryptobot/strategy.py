"""Adaptive long/short strategy: detects the market regime and switches tactics.

* Trend regime (ADX high and rising) -> trend following in the trend's direction:
  long when EMA fast > slow, price above the long-term EMA and +DI > -DI;
  short on the mirror image. Exit on EMA cross-back or the ATR trailing stop.
* Range regime (ADX low) -> mean reversion: long below the lower Bollinger band
  with RSI oversold, short above the upper band with RSI overbought; take
  profit at the middle band.
* Neutral (in between) -> no new entries, only manage open positions.

On spot markets set ``allow_short: false`` (long-only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from . import indicators as ind

TREND, RANGE, NEUTRAL = "trend", "range", "neutral"
LONG, SHORT = "long", "short"
ENTER, EXIT, HOLD, ADAPT = "enter", "exit", "hold", "adapt"


@dataclass
class Decision:
    action: str  # ENTER | EXIT | HOLD | ADAPT
    regime: str
    reason: str = ""
    side: Optional[str] = None  # LONG | SHORT for ENTER
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None


class AdaptiveStrategy:
    def __init__(self, params: dict):
        self.p = params

    @property
    def warmup(self) -> int:
        p = self.p
        return max(p["ema_trend"], p["ema_slow"], p["bb_period"], 2 * p["adx_period"]) + 5

    def analyze(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.p
        out = df.copy()
        close = out["close"]
        out["ema_fast"] = ind.ema(close, p["ema_fast"])
        out["ema_slow"] = ind.ema(close, p["ema_slow"])
        out["ema_trend"] = ind.ema(close, p["ema_trend"])
        out["rsi"] = ind.rsi(close, p["rsi_period"])
        out["atr"] = ind.atr(out, p["atr_period"])
        out = out.join(ind.adx(out, p["adx_period"]))
        out = out.join(ind.bollinger(close, p["bb_period"], p["bb_std"]))
        return out

    def regime(self, row) -> str:
        if row["adx"] >= self.p["adx_trend"]:
            return TREND
        if row["adx"] <= self.p["adx_range"]:
            return RANGE
        return NEUTRAL

    COLUMNS = ("close", "atr", "rsi", "adx", "plus_di", "minus_di", "ema_fast", "ema_slow",
               "ema_trend", "bb_lower", "bb_mid", "bb_upper")

    @classmethod
    def columns(cls, analyzed: pd.DataFrame) -> dict:
        """Column arrays for fast bar-by-bar access."""
        return {c: analyzed[c].to_numpy() for c in cls.COLUMNS}

    def decide(self, analyzed: pd.DataFrame, position=None) -> Decision:
        """Decide using the last *closed* candle of an analyzed frame."""
        return self.decide_at(self.columns(analyzed), len(analyzed) - 1, position)

    def decide_at(self, cols: dict, i: int, position=None) -> Decision:
        """Decide using bar ``i`` (only data up to and including ``i`` is read)."""
        if i + 1 < self.warmup:
            return Decision(HOLD, NEUTRAL, "warming up")
        p = self.p
        row = {c: cols[c][i] for c in self.COLUMNS}
        regime = self.regime(row)
        close, atr, rsi = row["close"], row["atr"], row["rsi"]
        stop_dist = p["stop_atr_mult"] * atr

        up_trend = (row["ema_fast"] > row["ema_slow"] and close > row["ema_trend"]
                    and row["plus_di"] > row["minus_di"])
        down_trend = (row["ema_fast"] < row["ema_slow"] and close < row["ema_trend"]
                      and row["minus_di"] > row["plus_di"])
        strong_bear = regime == TREND and row["minus_di"] > row["plus_di"]
        strong_bull = regime == TREND and row["plus_di"] > row["minus_di"]
        lookback = max(1, int(p.get("adx_rising_bars", 3)))
        adx_rising = not p.get("adx_rising", True) or row["adx"] > cols["adx"][i - lookback]
        trend_filter = p.get("range_trend_filter", True)
        allow_long = p.get("allow_long", True)
        allow_short = p.get("allow_short", True)

        if position is None:
            if regime == TREND and adx_rising:
                if allow_long and up_trend and rsi < p["rsi_overbought"]:
                    return Decision(ENTER, TREND, f"trend long ADX={row['adx']:.1f}",
                                    side=LONG, stop_price=close - stop_dist)
                if allow_short and down_trend and rsi > p["rsi_oversold"]:
                    return Decision(ENTER, TREND, f"trend short ADX={row['adx']:.1f}",
                                    side=SHORT, stop_price=close + stop_dist)
            if regime == RANGE:
                if (allow_long and close < row["bb_lower"] and rsi < p["rsi_oversold"]
                        and (not trend_filter or close > row["ema_trend"])):
                    return Decision(ENTER, RANGE, f"range long RSI={rsi:.1f}", side=LONG,
                                    stop_price=close - stop_dist, take_profit=row["bb_mid"])
                if (allow_short and close > row["bb_upper"] and rsi > p["rsi_overbought"]
                        and (not trend_filter or close < row["ema_trend"])):
                    return Decision(ENTER, RANGE, f"range short RSI={rsi:.1f}", side=SHORT,
                                    stop_price=close + stop_dist, take_profit=row["bb_mid"])
            return Decision(HOLD, regime, "no setup")

        # --- managing an open position ---
        long = position.side == LONG
        if position.regime == TREND:
            crossed_back = row["ema_fast"] < row["ema_slow"] if long else row["ema_fast"] > row["ema_slow"]
            if crossed_back:
                return Decision(EXIT, regime, "trend exit: EMA cross back")
            if p.get("trend_exit_on_di", False) and (strong_bear if long else strong_bull):
                return Decision(EXIT, regime, "trend exit: opposite DI")
            return Decision(HOLD, regime, "riding trend")

        # range position
        if strong_bear if long else strong_bull:
            return Decision(EXIT, regime, "range exit: strong trend against position")
        if (rsi > p["rsi_overbought"]) if long else (rsi < p["rsi_oversold"]):
            return Decision(EXIT, regime, "range exit: RSI reached opposite extreme")
        if regime == TREND and (up_trend if long else down_trend):
            return Decision(ADAPT, TREND, "range position upgraded to trend mode")
        return Decision(HOLD, regime, "waiting for mean reversion", take_profit=row["bb_mid"])
