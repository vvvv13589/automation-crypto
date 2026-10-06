"""Adaptive strategy: detects the market regime and switches tactics.

* Trend regime (ADX high)  -> trend following: EMA fast > slow, price above the
  long-term EMA, +DI > -DI. Exit on EMA cross-down or ATR trailing stop.
* Range regime (ADX low)   -> mean reversion: buy when price pierces the lower
  Bollinger band with RSI oversold; take profit at the middle band.
* Neutral (in between)     -> no new entries, only manage open positions.

Spot, long-only: a "sell" signal means "close the long", never short.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from . import indicators as ind

TREND, RANGE, NEUTRAL = "trend", "range", "neutral"
BUY, SELL, HOLD, ADAPT = "buy", "sell", "hold", "adapt"


@dataclass
class Decision:
    action: str  # BUY | SELL | HOLD | ADAPT
    regime: str
    reason: str = ""
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

    def regime(self, row: pd.Series) -> str:
        if row["adx"] >= self.p["adx_trend"]:
            return TREND
        if row["adx"] <= self.p["adx_range"]:
            return RANGE
        return NEUTRAL

    def decide(self, analyzed: pd.DataFrame, position=None) -> Decision:
        """Decide using the last *closed* candle of an analyzed frame."""
        if len(analyzed) < self.warmup:
            return Decision(HOLD, NEUTRAL, "warming up")
        p = self.p
        row = analyzed.iloc[-1]
        regime = self.regime(row)
        close, atr = row["close"], row["atr"]
        bull_trend = (
            row["ema_fast"] > row["ema_slow"]
            and close > row["ema_trend"]
            and row["plus_di"] > row["minus_di"]
        )
        bear_trend = regime == TREND and row["minus_di"] > row["plus_di"]

        if position is None:
            if regime == TREND and bull_trend and row["rsi"] < p["rsi_overbought"]:
                return Decision(
                    BUY, TREND, f"trend entry ADX={row['adx']:.1f}",
                    stop_price=close - p["stop_atr_mult"] * atr,
                )
            if (
                regime == RANGE
                and close < row["bb_lower"]
                and row["rsi"] < p["rsi_oversold"]
            ):
                return Decision(
                    BUY, RANGE, f"range entry RSI={row['rsi']:.1f}",
                    stop_price=close - p["stop_atr_mult"] * atr,
                    take_profit=row["bb_mid"],
                )
            return Decision(HOLD, regime, "no setup")

        # --- managing an open long ---
        if position.regime == TREND:
            if row["ema_fast"] < row["ema_slow"]:
                return Decision(SELL, regime, "trend exit: EMA cross down")
            if bear_trend:
                return Decision(SELL, regime, "trend exit: bearish DI")
            return Decision(HOLD, regime, "riding trend")

        # range position
        if bear_trend:
            return Decision(SELL, regime, "range exit: market turned bearish trend")
        if row["rsi"] > p["rsi_overbought"]:
            return Decision(SELL, regime, "range exit: RSI overbought")
        if regime == TREND and bull_trend:
            # Mean-reversion trade turned into a breakout: let it run.
            return Decision(ADAPT, TREND, "range position upgraded to trend mode")
        return Decision(HOLD, regime, "waiting for mean reversion", take_profit=row["bb_mid"])
