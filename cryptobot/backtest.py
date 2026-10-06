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
    halted_at: str | None = None

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
            key = f"{t.side}_{t.regime}"
            by_regime[key] = by_regime.get(key, 0) + 1
        fees = sum(t.fees for t in self.trades)
        funding = sum(t.funding for t in self.trades)
        return {
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(float(eq.iloc[-1]), 2),
            "total_return_pct": round((eq.iloc[-1] / self.start_equity - 1) * 100, 2),
            "buy_hold_return_pct": round(self.buy_hold_return * 100, 2),
            "max_drawdown_pct": round(float(dd.min()) * 100, 2),
            "sharpe": round(float(sharpe), 2),
            "trades": len(pnls),
            "trades_by_type": by_regime,
            "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else 0.0,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "fees_paid": round(fees, 2),
            "funding_paid": round(funding, 2),
            "max_drawdown_halt_at": self.halted_at,
            "pnl_before_costs": round(sum(pnls) + fees + funding, 2),
        }


def _bars_per_year(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 1.0
    step = (index[-1] - index[0]).total_seconds() / (len(index) - 1)
    return 365 * 86400 / step if step > 0 else 1.0


def run_backtest(cfg: dict, candles: pd.DataFrame) -> BacktestResult:
    trader = Trader(cfg, make_paper_broker(cfg))
    analyzed = trader.strategy.analyze(candles)  # indicators are causal, so precompute once
    warmup = trader.strategy.warmup
    equity_points = []
    # Decisions happen when a bar closes, i.e. at open time + bar length.
    step = analyzed.index.to_series().diff().median() if len(analyzed) > 1 else pd.Timedelta(0)

    cols = trader.strategy.columns(analyzed)
    opens, highs, lows = (analyzed[c].to_numpy() for c in ("open", "high", "low"))
    closes = cols["close"]
    stamps = list(analyzed.index)

    for i in range(len(analyzed)):
        ts = stamps[i]
        if i >= warmup:
            had_position = trader.position is not None
            # 1) working limit entry from the previous close: did price reach it?
            if trader.check_pending(lows[i], highs[i], now=ts):
                # filled inside this bar: only the (pessimistic) stop can also trigger
                trader.check_exits(lows[i], highs[i], now=ts, intrabar=True, stop_only=True)
            # 2) intrabar stops/targets for a position opened on a previous bar
            elif had_position:
                trader.check_exits(lows[i], highs[i], now=ts, open_=opens[i], intrabar=True)
            # 3) bar closed -> strategy decision on data up to and including this bar
            trader.on_bar(cols, i, now=ts + step)
        equity_points.append(trader.equity(closes[i]))

    if trader.pending is not None:
        trader._cancel_pending(analyzed.index[-1], "end of backtest")
    if trader.position is not None:  # mark-to-market close at the end
        trader._close(float(analyzed["close"].iloc[-1]), analyzed.index[-1], "end of backtest")
        equity_points[-1] = trader.equity(float(analyzed["close"].iloc[-1]))

    equity = pd.Series(equity_points, index=analyzed.index)
    first = analyzed["close"].iloc[min(warmup, len(analyzed) - 1)]
    bh = analyzed["close"].iloc[-1] / first - 1
    return BacktestResult(equity.iloc[warmup:], trader.trades, cfg["paper"]["starting_cash"],
                          float(bh), trader.halted_at)


def make_paper_broker(cfg: dict) -> PaperBroker:
    p = cfg["paper"]
    leverage = float(cfg["futures"]["leverage"]) if cfg.get("market") == "future" else 1.0
    return PaperBroker(p["starting_cash"], p["maker_fee"], p["taker_fee"], p["slippage"], leverage)
