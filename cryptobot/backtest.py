"""Event-driven backtest that reuses the exact live Trader logic."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .broker import PaperBroker
from .trader import Trader


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: list
    start_equity: float
    buy_hold_return: float

    def summary(self) -> dict:
        eq = self.equity
        rets = eq.pct_change().dropna()
        dd = eq / eq.cummax() - 1
        pnls = [t.pnl for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        bars_per_year = _bars_per_year(eq.index)
        sharpe = (rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 0 else 0.0
        by_regime: dict[str, int] = {}
        for t in self.trades:
            by_regime[t.regime] = by_regime.get(t.regime, 0) + 1
        return {
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(float(eq.iloc[-1]), 2),
            "total_return_pct": round((eq.iloc[-1] / self.start_equity - 1) * 100, 2),
            "buy_hold_return_pct": round(self.buy_hold_return * 100, 2),
            "max_drawdown_pct": round(float(dd.min()) * 100, 2),
            "sharpe": round(float(sharpe), 2),
            "trades": len(pnls),
            "trades_by_regime": by_regime,
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        }


def _bars_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 1.0
    step = (index[-1] - index[0]).total_seconds() / (len(index) - 1)
    return 365 * 86400 / step if step > 0 else 1.0


def run_backtest(cfg: dict, candles: pd.DataFrame) -> BacktestResult:
    paper = cfg["paper"]
    broker = PaperBroker(paper["starting_cash"], paper["fee_rate"], paper["slippage"])
    trader = Trader(cfg, broker)
    analyzed = trader.strategy.analyze(candles)  # indicators are causal, so precompute once
    warmup = trader.strategy.warmup
    equity_points = []

    for i in range(len(analyzed)):
        bar = analyzed.iloc[i]
        ts = analyzed.index[i]
        if i >= warmup:
            # 1) intrabar stops/targets for a position opened on a previous bar
            trader.check_exits(bar["low"], bar["high"], now=ts, open_=bar["open"])
            # 2) bar closed -> strategy decision on data up to and including this bar
            trader.on_analyzed(analyzed.iloc[: i + 1], now=ts)
        equity_points.append(trader.equity(bar["close"]))

    if trader.position is not None:  # mark-to-market close at the end
        trader._close(float(analyzed["close"].iloc[-1]), analyzed.index[-1], "end of backtest")
        equity_points[-1] = trader.equity(float(analyzed["close"].iloc[-1]))

    equity = pd.Series(equity_points, index=analyzed.index)
    first = analyzed["close"].iloc[min(warmup, len(analyzed) - 1)]
    bh = analyzed["close"].iloc[-1] / first - 1
    return BacktestResult(equity.iloc[warmup:], trader.trades, paper["starting_cash"], float(bh))
