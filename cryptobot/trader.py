"""Core trading engine shared by backtest, paper and live modes."""

from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from .risk import RiskManager
from .strategy import ADAPT, BUY, RANGE, SELL, TREND, AdaptiveStrategy

log = logging.getLogger(__name__)


@dataclass
class Position:
    amount: float
    entry_price: float
    stop_price: float
    take_profit: Optional[float]
    regime: str
    highest: float
    opened_at: str
    cost: float  # quote spent including fees


@dataclass
class Trade:
    opened_at: str
    closed_at: str
    regime: str
    entry_price: float
    exit_price: float
    amount: float
    pnl: float
    pnl_pct: float
    fees: float
    reason: str


class Trader:
    def __init__(self, cfg: dict, broker, state_path: str | None = None,
                 journal_path: str | None = None):
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.strategy = AdaptiveStrategy(cfg["strategy"])
        self.risk = RiskManager(cfg["risk"])
        self.broker = broker
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.last_atr: Optional[float] = None
        self.last_price: Optional[float] = None
        self.cooldown = 0  # candles to wait after a close before re-entering
        self.trade_day: Optional[str] = None  # local date for the day-trade counter
        self.trades_today = 0
        self.state_path = state_path
        self.journal_path = journal_path
        self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        with open(self.state_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("position"):
            self.position = Position(**data["position"])
        self.risk.load(data.get("risk", {}))
        self.last_atr = data.get("last_atr")
        self.cooldown = int(data.get("cooldown", 0))
        self.trade_day = data.get("trade_day")
        self.trades_today = int(data.get("trades_today", 0))
        log.info("restored state: position=%s", self.position)

    def _save_state(self) -> None:
        if not self.state_path:
            return
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "position": asdict(self.position) if self.position else None,
                    "risk": self.risk.to_dict(),
                    "last_atr": self.last_atr,
                    "cooldown": self.cooldown,
                    "trade_day": self.trade_day,
                    "trades_today": self.trades_today,
                },
                fh,
                indent=2,
            )
        os.replace(tmp, self.state_path)

    def _journal(self, trade: Trade) -> None:
        if not self.journal_path:
            return
        Path(self.journal_path).parent.mkdir(parents=True, exist_ok=True)
        new = not os.path.exists(self.journal_path)
        with open(self.journal_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(asdict(trade)))
            if new:
                writer.writeheader()
            writer.writerow(asdict(trade))

    # ---------------------------------------------------------------- helpers
    def equity(self, price: float) -> float:
        cash, base = self.broker.balances(self.symbol)
        return cash + base * price

    @staticmethod
    def _ts(now) -> str:
        if isinstance(now, pd.Timestamp):
            now = now.to_pydatetime()
        return (now or datetime.now(timezone.utc)).isoformat()

    # -------------------------------------------------------------- day trade
    @property
    def daytrade(self) -> dict:
        dt = self.cfg.get("daytrade") or {}
        return dt if dt.get("enabled") else {}

    def _local(self, now) -> pd.Timestamp:
        ts = pd.Timestamp(now)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.tz_convert(self.daytrade.get("timezone", "Asia/Taipei"))

    @staticmethod
    def _hhmm(value: str) -> time:
        h, m = str(value).split(":")
        return time(int(h), int(m))

    def session_check(self, price: float, now) -> bool:
        """Day-trade mode: flatten at session end or once the local day rolls over."""
        if not self.daytrade or self.position is None:
            return False
        local = self._local(now)
        opened = self._local(self.position.opened_at)
        if local.date() != opened.date() or local.time() >= self._hhmm(self.daytrade["session_end"]):
            self._close(price, now, "day-trade session close")
            return True
        return False

    def _entry_window(self, now) -> tuple[bool, str]:
        dt = self.daytrade
        if not dt:
            return True, ""
        local = self._local(now)
        day = local.strftime("%Y-%m-%d")
        if day != self.trade_day:
            self.trade_day, self.trades_today = day, 0
        t = local.time()
        if t < self._hhmm(dt.get("session_start", "00:00")):
            return False, "before day-trade session start"
        if t >= self._hhmm(dt["no_new_entries_after"]) or t >= self._hhmm(dt["session_end"]):
            return False, "too close to day-trade session end"
        if self.trades_today >= int(dt.get("max_trades_per_day", 10**9)):
            return False, "max trades per day reached"
        return True, ""

    # ------------------------------------------------------------------ exits
    def check_exits(self, low: float, high: float, now=None, open_: float | None = None) -> bool:
        """Intrabar / real-time stop-loss, trailing and take-profit checks.

        For live trading call with low == high == latest price.
        Returns True if the position was closed.
        """
        pos = self.position
        if pos is None:
            return False
        if low <= pos.stop_price:
            # If the bar gapped below the stop, we get the (worse) open price.
            price = min(pos.stop_price, open_) if open_ is not None else low
            kind = "trailing stop" if pos.stop_price > pos.entry_price else "stop loss"
            self._close(price, now, kind)
            return True
        if pos.take_profit is not None and high >= pos.take_profit:
            price = max(pos.take_profit, open_) if open_ is not None else high
            self._close(price, now, "take profit")
            return True
        if high > pos.highest:
            pos.highest = high
        return False

    # -------------------------------------------------------------- on candle
    def on_candle(self, candles: pd.DataFrame, now=None) -> str:
        """Run the strategy on closed candles. Returns the action taken."""
        analyzed = self.strategy.analyze(candles)
        return self.on_analyzed(analyzed, now)

    def on_analyzed(self, analyzed: pd.DataFrame, now=None) -> str:
        row = analyzed.iloc[-1]
        price = float(row["close"])
        self.last_price = price
        self.last_atr = float(row["atr"]) if pd.notna(row["atr"]) else self.last_atr
        now = now if now is not None else analyzed.index[-1]
        self.risk.update(self.equity(price), pd.Timestamp(now).to_pydatetime())

        if self.session_check(price, now):
            self._save_state()
            return "sell"

        decision = self.strategy.decide(analyzed, self.position)
        action = "hold"
        pos = self.position

        if pos is not None:
            if decision.action == SELL:
                self._close(price, now, decision.reason)
                action = "sell"
            else:
                if decision.action == ADAPT:
                    pos.regime, pos.take_profit = TREND, None
                    log.info("position adapted to trend mode")
                    action = "adapt"
                elif pos.regime == RANGE and decision.take_profit is not None:
                    pos.take_profit = float(decision.take_profit)  # follow the moving middle band
                if pos.regime == TREND and self.last_atr:
                    trail = pos.highest - self.cfg["strategy"]["trail_atr_mult"] * self.last_atr
                    if trail > pos.stop_price:
                        pos.stop_price = trail
        elif self.cooldown > 0:
            self.cooldown -= 1
        elif decision.action == BUY:
            ok, why = self.risk.can_open()
            if ok:
                ok, why = self._entry_window(now)
            if not ok:
                log.info("entry skipped: %s", why)
            elif self._open(price, decision, now):
                self.trades_today += 1
                action = "buy"

        self._save_state()
        return action

    # --------------------------------------------------------- order helpers
    def _open(self, price: float, decision, now) -> bool:
        cash, _ = self.broker.balances(self.symbol)
        equity = self.equity(price)
        amount = self.risk.position_size(equity, cash, price, decision.stop_price)
        if amount <= 0:
            return False
        fill = self.broker.market_buy(self.symbol, amount, price)
        if fill is None or fill.amount <= 0:
            return False
        # Keep the stop the same distance below the actual fill.
        stop = fill.price - (price - decision.stop_price)
        self.position = Position(
            amount=fill.amount,
            entry_price=fill.price,
            stop_price=stop,
            take_profit=decision.take_profit,
            regime=decision.regime,
            highest=fill.price,
            opened_at=self._ts(now),
            cost=fill.amount * fill.price + fill.fee,
        )
        log.info("BUY %.6f %s @ %.2f [%s] stop=%.2f tp=%s | %s", fill.amount, self.symbol,
                 fill.price, decision.regime, stop, decision.take_profit, decision.reason)
        self._save_state()
        return True

    def _close(self, price: float, now, reason: str) -> None:
        pos = self.position
        fill = self.broker.market_sell(self.symbol, pos.amount, price)
        if fill is None:
            log.error("could not close position (order rejected); will retry")
            return
        proceeds = fill.amount * fill.price - fill.fee
        frac = fill.amount / pos.amount
        pnl = proceeds - pos.cost * frac
        fees = (pos.cost - pos.amount * pos.entry_price) * frac + fill.fee
        trade = Trade(
            opened_at=pos.opened_at,
            closed_at=self._ts(now),
            regime=pos.regime,
            entry_price=pos.entry_price,
            exit_price=fill.price,
            amount=fill.amount,
            pnl=pnl,
            pnl_pct=pnl / pos.cost * 100 if pos.cost else 0.0,
            fees=fees,
            reason=reason,
        )
        self.trades.append(trade)
        self._journal(trade)
        log.info("SELL %.6f %s @ %.2f pnl=%.2f (%.2f%%) | %s", fill.amount, self.symbol,
                 fill.price, pnl, trade.pnl_pct, reason)
        self.position = None
        self.cooldown = int(self.cfg["risk"].get("cooldown_bars", 0))
        self._save_state()
