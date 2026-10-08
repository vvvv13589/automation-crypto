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

import numpy as np
import pandas as pd

from . import indicators as ind

TREND, RANGE, NEUTRAL, PULLBACK = "trend", "range", "neutral", "pullback"
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

    htf_timeframe = None  # single-timeframe strategy

    def history_warmup(self, base_step: pd.Timedelta) -> int:
        """Candles of history a backtest needs before the first decision."""
        return self.warmup

    def analyze(self, df: pd.DataFrame, htf: pd.DataFrame | None = None) -> pd.DataFrame:
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


def _timedelta(timeframe: str) -> pd.Timedelta:
    units = {"m": "min", "h": "h", "d": "D", "w": "W"}
    return pd.Timedelta(int(timeframe[:-1]), unit=units[timeframe[-1]])


def htf_trend(df: pd.DataFrame, timeframe: str, fast: int, slow: int,
              htf: pd.DataFrame | None = None) -> np.ndarray:
    """Higher-timeframe trend (+1 up, -1 down, 0 none) for every base candle.

    Only *closed* higher-timeframe candles are used: an HTF candle becomes
    visible to a base candle once the base candle's close time reaches the
    HTF candle's close time. ``htf`` may be supplied (live: fetched from the
    exchange); otherwise it is resampled from ``df`` (backtests).
    """
    rule = _timedelta(timeframe)
    if htf is None:
        htf = df.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    close = htf["close"]
    ef, es = ind.ema(close, fast), ind.ema(close, slow)
    trend = np.where((ef > es) & (close > es), 1, np.where((ef < es) & (close < es), -1, 0))
    trend[: slow] = 0  # EMA not settled yet
    base_step = df.index.to_series().diff().median() if len(df) > 1 else pd.Timedelta(0)
    left = pd.DataFrame({"t": df.index + base_step})
    right = pd.DataFrame({"t": htf.index + rule, "trend": trend})
    merged = pd.merge_asof(left, right, on="t", direction="backward")
    return merged["trend"].fillna(0).to_numpy()


class PullbackStrategy:
    """Trade with the higher-timeframe trend, enter on a pullback.

    * Direction: HTF (default 4h) EMA fast > slow and close above slow -> longs only;
      the mirror image -> shorts only; otherwise stand aside.
    * Entry (long): within the last ``pullback_lookback`` candles RSI dipped below
      ``pullback_rsi`` (the pullback), and now RSI crosses back above 50 with the
      close above the base EMA (the pullback is over). Shorts mirror this.
    * Stop just beyond the pullback's swing low/high (clamped to min/max ATR),
      take profit at ``take_profit_r`` x risk (0 = no target, ATR trailing stop),
      stop moved to break-even at ``breakeven_r`` x risk (handled by the Trader).
    * Exit early if the HTF trend flips against the position.
    """

    COLUMNS = ("close", "atr", "rsi", "ema_pb", "htf_trend", "rsi_min", "rsi_max",
               "swing_low", "swing_high")

    def __init__(self, params: dict):
        self.p = params

    @property
    def htf_timeframe(self) -> str:
        return self.p.get("htf_timeframe", "4h")

    @property
    def warmup(self) -> int:
        p = self.p
        return max(p.get("pullback_ema", 20), p["rsi_period"], p["atr_period"]) + p.get("pullback_lookback", 8) + 5

    def history_warmup(self, base_step: pd.Timedelta) -> int:
        """Backtests resample the HTF from base candles, so they also need the
        HTF EMA to settle; live trading fetches HTF candles separately."""
        if base_step <= pd.Timedelta(0):
            return self.warmup
        ratio = _timedelta(self.htf_timeframe) / base_step
        return max(self.warmup, int((self.p.get("htf_ema_slow", 50) + 2) * ratio))

    @classmethod
    def columns(cls, analyzed: pd.DataFrame) -> dict:
        return {c: analyzed[c].to_numpy() for c in cls.COLUMNS}

    def analyze(self, df: pd.DataFrame, htf: pd.DataFrame | None = None) -> pd.DataFrame:
        p = self.p
        lb = int(p.get("pullback_lookback", 8))
        out = df.copy()
        out["rsi"] = ind.rsi(out["close"], p["rsi_period"])
        out["atr"] = ind.atr(out, p["atr_period"])
        out["ema_pb"] = ind.ema(out["close"], p.get("pullback_ema", 20))
        out["rsi_min"] = out["rsi"].rolling(lb).min().shift(1)  # pullback before this candle
        out["rsi_max"] = out["rsi"].rolling(lb).max().shift(1)
        out["swing_low"] = out["low"].rolling(lb + 1).min()
        out["swing_high"] = out["high"].rolling(lb + 1).max()
        out["htf_trend"] = htf_trend(df, self.htf_timeframe, p.get("htf_ema_fast", 20),
                                     p.get("htf_ema_slow", 50), htf)
        return out

    def decide(self, analyzed: pd.DataFrame, position=None) -> Decision:
        return self.decide_at(self.columns(analyzed), len(analyzed) - 1, position)

    def decide_at(self, cols: dict, i: int, position=None) -> Decision:
        if i + 1 < self.warmup or i < 1:
            return Decision(HOLD, NEUTRAL, "warming up")
        p = self.p
        trend = cols["htf_trend"][i]
        label = {1: "htf up", -1: "htf down"}.get(int(trend), "htf flat")

        if position is not None:
            against = trend == (-1 if position.side == LONG else 1)
            if against:
                return Decision(EXIT, PULLBACK, "exit: higher-timeframe trend flipped")
            return Decision(HOLD, PULLBACK, label)

        close, atr, rsi, prev_rsi = (cols["close"][i], cols["atr"][i], cols["rsi"][i], cols["rsi"][i - 1])
        if not atr == atr or atr <= 0:  # NaN / zero
            return Decision(HOLD, PULLBACK, "no ATR")
        depth = p.get("pullback_rsi", 40)
        min_d, max_d = p.get("min_stop_atr", 1.0) * atr, p.get("max_stop_atr", 3.0) * atr
        tp_r = p.get("take_profit_r", 2.0)

        if trend == 1 and p.get("allow_long", True):
            if (cols["rsi_min"][i] < depth and prev_rsi <= 50 < rsi and close > cols["ema_pb"][i]):
                stop = min(cols["swing_low"][i] - 0.25 * atr, close - min_d)
                if close - stop <= max_d:
                    tp = close + tp_r * (close - stop) if tp_r > 0 else None
                    return Decision(ENTER, PULLBACK, f"pullback long ({label})", side=LONG,
                                    stop_price=stop, take_profit=tp)
        if trend == -1 and p.get("allow_short", True):
            if (cols["rsi_max"][i] > 100 - depth and prev_rsi >= 50 > rsi and close < cols["ema_pb"][i]):
                stop = max(cols["swing_high"][i] + 0.25 * atr, close + min_d)
                if stop - close <= max_d:
                    tp = close - tp_r * (stop - close) if tp_r > 0 else None
                    return Decision(ENTER, PULLBACK, f"pullback short ({label})", side=SHORT,
                                    stop_price=stop, take_profit=tp)
        return Decision(HOLD, PULLBACK, label)


STRATEGIES = {"adaptive": AdaptiveStrategy, "pullback": PullbackStrategy}


def make_strategy(params: dict):
    name = params.get("name", "breakout")
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; choose from {sorted(STRATEGIES)}")
    return STRATEGIES[name](params)


class BreakoutStrategy:
    """Donchian channel breakout (classic trend following), long and short.

    * Long when the close breaks above the highest high of the previous
      ``bo_n`` candles; short when it breaks below the lowest low.
    * Initial stop ``stop_atr_mult`` x ATR away; no profit target - the Trader
      trails the stop ``trail_atr_mult`` x ATR behind the best price, so
      winners can run for days or weeks.

    Research (Binance futures 2024-01..2026-10, 4h): profitable for 98%/100%/91%
    of 45 parameter combinations on BTC/ETH/SOL. Modest returns, ~20% drawdowns.
    """

    COLUMNS = ("close", "atr", "dc_hi", "dc_lo", "vol_ratio")
    htf_timeframe = None

    def __init__(self, params: dict):
        self.p = params

    @property
    def warmup(self) -> int:
        vol_lb = self.p.get("bo_volume_lookback", 180) if self.p.get("bo_min_volume_ratio", 0) else 0
        return max(self.p.get("bo_n", 40) + self.p["atr_period"], vol_lb) + 5

    def history_warmup(self, base_step: pd.Timedelta) -> int:
        return self.warmup

    @classmethod
    def columns(cls, analyzed: pd.DataFrame) -> dict:
        return {c: analyzed[c].to_numpy() for c in cls.COLUMNS}

    def analyze(self, df: pd.DataFrame, htf: pd.DataFrame | None = None) -> pd.DataFrame:
        n = self.p.get("bo_n", 40)
        out = df.copy()
        out["atr"] = ind.atr(out, self.p["atr_period"])
        out["dc_hi"] = out["high"].rolling(n).max().shift(1)  # channel of the *previous* n candles
        out["dc_lo"] = out["low"].rolling(n).min().shift(1)
        # breakout candle volume vs the average of the previous `bo_volume_lookback` candles
        lb = self.p.get("bo_volume_lookback", 180)
        out["vol_ratio"] = out["volume"] / out["volume"].rolling(lb, min_periods=lb // 2).mean().shift(1)
        return out

    def decide(self, analyzed: pd.DataFrame, position=None) -> Decision:
        return self.decide_at(self.columns(analyzed), len(analyzed) - 1, position)

    def decide_at(self, cols: dict, i: int, position=None) -> Decision:
        if i + 1 < self.warmup:
            return Decision(HOLD, NEUTRAL, "warming up")
        if position is not None:
            return Decision(HOLD, TREND, "trailing stop manages the exit")
        close, atr = cols["close"][i], cols["atr"][i]
        if not atr == atr or atr <= 0:
            return Decision(HOLD, NEUTRAL, "no ATR")
        dist = self.p["stop_atr_mult"] * atr
        min_vr = self.p.get("bo_min_volume_ratio", 0) or 0
        if min_vr and not cols["vol_ratio"][i] >= min_vr:  # also rejects NaN
            return Decision(HOLD, TREND, "no volume confirmation")
        if close > cols["dc_hi"][i] and self.p.get("allow_long", True):
            return Decision(ENTER, TREND, f"breakout above {self.p.get('bo_n', 40)}-bar high",
                            side=LONG, stop_price=close - dist)
        if close < cols["dc_lo"][i] and self.p.get("allow_short", True):
            return Decision(ENTER, TREND, f"breakdown below {self.p.get('bo_n', 40)}-bar low",
                            side=SHORT, stop_price=close + dist)
        return Decision(HOLD, TREND, "inside channel")


STRATEGIES["breakout"] = BreakoutStrategy
