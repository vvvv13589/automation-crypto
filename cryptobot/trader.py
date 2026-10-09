"""Core trading engine shared by backtest, paper and live modes."""

from __future__ import annotations

import csv
import dataclasses
import json
import logging
import os
import time as time_module
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from .risk import RiskManager
from .strategy import ADAPT, ENTER, EXIT, LONG, RANGE, TREND, make_strategy

log = logging.getLogger(__name__)

FUNDING_HOURS_UTC = (0, 8, 16)  # Binance perpetual funding times


def _sign(side: str) -> int:
    return 1 if side == LONG else -1


@dataclass
class Position:
    side: str
    amount: float
    entry_price: float
    stop_price: float
    take_profit: Optional[float]
    regime: str
    best: float  # most favourable price seen (high for long, low for short)
    opened_at: str
    entry_fee: float = 0.0
    funding: float = 0.0
    last_funding_check: Optional[str] = None
    stop_order_id: Optional[str] = None
    risk_dist: float = 0.0  # initial entry-to-stop distance (1R)
    exchange_stop: Optional[float] = None  # stop price actually working on the exchange


@dataclass
class PendingEntry:
    order_id: str
    side: str
    amount: float
    price: float
    stop_dist: float
    take_profit: Optional[float]
    regime: str
    bars_left: int
    reason: str


@dataclass
class Trade:
    side: str
    opened_at: str
    closed_at: str
    regime: str
    entry_price: float
    exit_price: float
    amount: float
    pnl: float
    pnl_pct: float  # % of position notional at entry
    fees: float
    funding: float
    reason: str
    r_multiple: float = 0.0  # pnl in units of the initial risk (1R = entry-to-stop loss)
    symbol: str = ""


def _from_dict(cls, data: dict):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


class Trader:
    def __init__(self, cfg: dict, broker, state_path: str | None = None,
                 journal_path: str | None = None, notify: Callable[[str], None] | None = None,
                 risk: RiskManager | None = None):
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.futures = cfg.get("market", "spot") == "future"
        self.leverage = float(cfg["futures"]["leverage"]) if self.futures else 1.0
        params = dict(cfg["strategy"])
        if not self.futures:
            params["allow_short"] = False
        self.strategy = make_strategy(params)
        # A scanner passes one RiskManager shared by every symbol (account-level breakers).
        self.shared_risk = risk is not None
        self.risk = risk or RiskManager(cfg["risk"])
        # Optional hook: entry_gate(trader, decision, cols, i) -> bool. A scanner uses it
        # to collect same-candle signals across symbols and rank them before entering.
        self.entry_gate: Callable | None = None
        self.broker = broker
        self.notify = notify or (lambda text: None)
        self.position: Optional[Position] = None
        self.pending: Optional[PendingEntry] = None
        self.trades: list[Trade] = []
        self.last_atr: Optional[float] = None
        self.cooldown = 0
        self.session_key: Optional[str] = None
        self.trades_today = 0
        self._halted = False
        self.halted_at: Optional[str] = None  # first time the circuit breaker stopped entries
        self._stop_error: Optional[str] = None  # last exchange-stop failure (notify once)
        self._stop_retry_at = 0.0
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
            pos = dict(data["position"])
            pos.setdefault("side", LONG)
            pos.setdefault("best", pos.get("highest", pos.get("entry_price")))
            self.position = _from_dict(Position, pos)
        if data.get("pending"):
            self.pending = _from_dict(PendingEntry, data["pending"])
        if not self.shared_risk:
            self.risk.load(data.get("risk", {}))
        self.last_atr = data.get("last_atr")
        self.cooldown = int(data.get("cooldown", 0))
        self.session_key = data.get("session_key")
        self.trades_today = int(data.get("trades_today", 0))
        if data.get("broker") and hasattr(self.broker, "load"):
            self.broker.load(data["broker"])
        if self.position or self.pending:
            log.info("%s restored: position=%s pending=%s", self.symbol, self.position, self.pending)

    def _save_state(self) -> None:
        if not self.state_path:
            return
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "symbol": self.symbol,
                    "position": asdict(self.position) if self.position else None,
                    "pending": asdict(self.pending) if self.pending else None,
                    "risk": self.risk.to_dict(),
                    "last_atr": self.last_atr,
                    "cooldown": self.cooldown,
                    "session_key": self.session_key,
                    "trades_today": self.trades_today,
                    "broker": self.broker.to_dict() if hasattr(self.broker, "to_dict") else None,
                },
                fh,
                indent=2,
            )
        os.replace(tmp, self.state_path)

    def _journal(self, trade: Trade) -> None:
        if not self.journal_path:
            return
        Path(self.journal_path).parent.mkdir(parents=True, exist_ok=True)
        fields = list(asdict(trade))
        new = not os.path.exists(self.journal_path)
        if not new:  # journal written by an older version: rewrite it with the current columns
            with open(self.journal_path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                if reader.fieldnames != fields:
                    rows = list(reader)
                    with open(self.journal_path, "w", newline="", encoding="utf-8") as out:
                        writer = csv.DictWriter(out, fieldnames=fields, restval="")
                        writer.writeheader()
                        writer.writerows({k: r.get(k, "") for k in fields} for r in rows)
        with open(self.journal_path, "a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields)
            if new:
                writer.writeheader()
            writer.writerow(asdict(trade))

    # ---------------------------------------------------------------- helpers
    def equity(self, price: float) -> float:
        return self.broker.equity(price)

    @staticmethod
    def _ts(now) -> str:
        if now is None:
            return datetime.now(timezone.utc).isoformat()
        return pd.Timestamp(now).isoformat()

    @staticmethod
    def _utc(now) -> pd.Timestamp:
        ts = pd.Timestamp(now)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    # -------------------------------------------------------------- day trade
    @property
    def daytrade(self) -> dict:
        dt = self.cfg.get("daytrade") or {}
        return dt if dt.get("enabled") else {}

    def _local(self, now) -> pd.Timestamp:
        return self._utc(now).tz_convert(self.daytrade.get("timezone", "Asia/Taipei"))

    @staticmethod
    def _hhmm(value: str) -> time:
        h, m = str(value).split(":")
        return time(int(h), int(m))

    def _session_of(self, now) -> str:
        """Trading-day label; a new day starts at ``session_end`` local time."""
        end = self._hhmm(self.daytrade["session_end"])
        shifted = self._local(now) - timedelta(hours=end.hour, minutes=end.minute)
        return shifted.strftime("%Y-%m-%d")

    def _minutes_to_session_end(self, now) -> float:
        local = self._local(now)
        end = self._hhmm(self.daytrade["session_end"])
        nxt = local.normalize() + timedelta(hours=end.hour, minutes=end.minute)
        if nxt <= local:
            nxt += timedelta(days=1)
        return (nxt - local).total_seconds() / 60

    def session_check(self, price: float, now) -> bool:
        """Day-trade mode: flatten once the trading day has rolled over."""
        if not self.daytrade:
            return False
        if self.pending and self._minutes_to_session_end(now) < self.daytrade["no_entry_minutes_before_end"]:
            self._cancel_pending(now, "day-trade entry window closed")
        if self.position is None:
            return False
        if self._session_of(now) != self._session_of(self.position.opened_at):
            self._close(price, now, "day-trade session close")
            return True
        return False

    def _entry_window(self, now) -> tuple[bool, str]:
        dt = self.daytrade
        if not dt:
            return True, ""
        key = self._session_of(now)
        if key != self.session_key:
            self.session_key, self.trades_today = key, 0
        if self._minutes_to_session_end(now) < dt["no_entry_minutes_before_end"]:
            return False, "too close to day-trade session end"
        if self.trades_today >= int(dt["max_trades_per_day"]):
            return False, "max trades per day reached"
        return True, ""

    # ---------------------------------------------------------------- funding
    def _apply_funding(self, now) -> None:
        """Paper/backtest only: perpetual funding, charged conservatively to either side."""
        pos = self.position
        if pos is None or not self.futures or self.broker.live:
            return
        rate = float(self.cfg["futures"].get("funding_rate", 0.0))
        now_utc = self._utc(now)
        last = self._utc(pos.last_funding_check or pos.opened_at)
        t = last.floor("h") + timedelta(hours=1)
        while t <= now_utc:
            if t.hour in FUNDING_HOURS_UTC:
                cost = pos.amount * pos.entry_price * rate
                self.broker.charge(cost)
                pos.funding += cost
            t += timedelta(hours=1)
        pos.last_funding_check = now_utc.isoformat()

    # ------------------------------------------------------------------ entry
    def check_pending(self, low: float, high: float, now=None) -> bool:
        """Has the working limit entry filled? Returns True if a position opened."""
        p = self.pending
        if p is None:
            return False
        status = self.broker.poll_limit(p.order_id, low, high)
        if not status.done:
            return False
        self.pending = None
        if status.filled > 0:
            self._on_entry_fill(p.side, status.filled, status.price, status.fee, p.stop_dist,
                                p.take_profit, p.regime, now, p.reason)
            return True
        log.info("limit entry %s not filled (expired / post-only rejected)", p.order_id)
        self._save_state()
        return False

    def _cancel_pending(self, now, why: str) -> None:
        p = self.pending
        if p is None:
            return
        status = self.broker.cancel_limit(p.order_id)
        self.pending = None
        log.info("limit entry cancelled: %s", why)
        if status.filled > 0:  # partially filled before the cancel
            self._on_entry_fill(p.side, status.filled, status.price, status.fee, p.stop_dist,
                                p.take_profit, p.regime, now, p.reason + " (partial)")
        self._save_state()

    def _open(self, price: float, decision, now) -> bool:
        equity = self.equity(price)
        free = self.broker.free_margin(price)
        amount = self.risk.position_size(equity, free, price, decision.stop_price, self.leverage)
        if amount <= 0:
            return False
        stop_dist = abs(price - decision.stop_price)
        orders = self.cfg.get("orders", {})
        use_limit = orders.get("entry_type", "market") == "limit" and self.broker.supports_limit
        if use_limit:
            offset = orders.get("limit_offset_bps", 0) / 10000 * price
            limit = price - offset if decision.side == LONG else price + offset
            oid = self.broker.place_limit(decision.side, amount, limit)
            if not oid:
                return False
            self.pending = PendingEntry(oid, decision.side, amount, limit, stop_dist,
                                        decision.take_profit, decision.regime,
                                        int(orders.get("limit_ttl_bars", 1)), decision.reason)
            log.info("LIMIT %s %.6f %s @ %.2f | %s", decision.side.upper(), amount, self.symbol,
                     limit, decision.reason)
            self._save_state()
            return True
        fill = self.broker.market_open(decision.side, amount, price)
        if fill is None or fill.amount <= 0:
            return False
        self._on_entry_fill(decision.side, fill.amount, fill.price, fill.fee, stop_dist,
                            decision.take_profit, decision.regime, now, decision.reason)
        return True

    def _on_entry_fill(self, side, amount, price, fee, stop_dist, take_profit, regime, now, reason):
        stop = price - _sign(side) * stop_dist  # same distance from the actual fill
        self.position = Position(
            side=side, amount=amount, entry_price=price, stop_price=stop,
            take_profit=take_profit, regime=regime, best=price, opened_at=self._ts(now),
            entry_fee=fee, last_funding_check=self._ts(now), risk_dist=stop_dist,
        )
        msg = (f"🟢 開倉 {side.upper()} {amount:.4f} {self.symbol} @ {price:.2f}\n"
               f"停損 {stop:.2f}" + (f" 停利 {take_profit:.2f}" if take_profit else "") +
               f"\n{reason}")
        log.info(msg.replace("\n", " | "))
        self.notify(msg)
        self._sync_exchange_stop(now)
        self._save_state()

    STOP_RETRY_SECONDS = 60

    def _wants_exchange_stop(self) -> bool:
        return bool(self.cfg.get("futures", {}).get("exchange_stop", True))

    def _sync_exchange_stop(self, now=None) -> None:
        """Mirror pos.stop_price on the exchange. A position with no exchange stop at all is
        closed at market; if only an update fails, the old stop keeps working and the update
        is retried from check_exits()."""
        pos = self.position
        if pos is None or not self._wants_exchange_stop():
            return
        self._stop_retry_at = time_module.monotonic() + self.STOP_RETRY_SECONDS
        try:
            pos.stop_order_id = self.broker.set_stop(pos.side, pos.amount, pos.stop_price,
                                                     pos.stop_order_id)
            pos.exchange_stop = pos.stop_price
            if self._stop_error:
                self.notify(f"✅ {self.symbol} 交易所停損單已更新為 {pos.stop_price:.6g}")
            self._stop_error = None
        except Exception as exc:
            log.error("%s: failed to place exchange stop: %s", self.symbol, exc)
            if pos.stop_order_id is None and self.broker.live:
                self.notify(f"⚠️ {self.symbol} 交易所停損單掛不上，持倉沒有保護 → 立即市價平倉\n{exc}")
                self._close(pos.stop_price, now, "exchange stop could not be placed")
            elif self._stop_error is None:
                self.notify(f"⚠️ {self.symbol} 停損單更新失敗，原本的停損單 "
                            f"{pos.exchange_stop or ''} 仍有效，每分鐘自動重試\n{exc}")
            self._stop_error = str(exc)

    # ------------------------------------------------------------------ exits
    def check_exits(self, low: float, high: float, now=None, open_: float | None = None,
                    intrabar: bool = False, stop_only: bool = False) -> bool:
        """Stop-loss / take-profit checks.

        Live: call with low == high == latest price (fills at that price).
        Backtest: ``intrabar=True`` fills at the stop/target level, or at the
        bar's open if it gapped through it.
        """
        pos = self.position
        if pos is None:
            return False
        s = _sign(pos.side)
        adverse, favourable = (low, high) if s > 0 else (high, low)

        if (adverse - pos.stop_price) * s <= 0:
            if intrabar:
                price = pos.stop_price
                if open_ is not None and (open_ - pos.stop_price) * s < 0:
                    price = open_
            else:
                price = adverse
            kind = "trailing / break-even stop" if (pos.stop_price - pos.entry_price) * s > 0 else "stop loss"
            self._close(price, now, kind)
            return True
        if not stop_only and pos.take_profit is not None and (favourable - pos.take_profit) * s >= 0:
            if intrabar:
                price = pos.take_profit
                if open_ is not None and (open_ - pos.take_profit) * s > 0:
                    price = open_
            else:
                price = favourable
            self._close(price, now, "take profit")
            return True
        if (favourable - pos.best) * s > 0:
            pos.best = favourable
        if (self.broker.live and self._wants_exchange_stop() and pos.exchange_stop != pos.stop_price
                and time_module.monotonic() >= self._stop_retry_at):
            self._sync_exchange_stop(now)  # an earlier stop update failed: retry
            self._save_state()
        return False

    def reconcile(self, price: float, now=None) -> None:
        """Live: detect a position closed on the exchange (protective stop, liquidation, manual)."""
        if not self.broker.live or self.position is None:
            return
        if abs(self.broker.position_amount()) <= 0:
            self._close(price, now, "closed on exchange (stop order / liquidation / manual)")

    # -------------------------------------------------------------- on candle
    def on_candle(self, candles: pd.DataFrame, now=None, htf: pd.DataFrame | None = None) -> str:
        """Run the strategy on closed candles. Returns the action taken."""
        return self.on_analyzed(self.strategy.analyze(candles, htf), now)

    def on_analyzed(self, analyzed: pd.DataFrame, now=None) -> str:
        now = now if now is not None else analyzed.index[-1]
        return self.on_bar(self.strategy.columns(analyzed), len(analyzed) - 1, now)

    def on_bar(self, cols: dict, i: int, now) -> str:
        """Bar ``i`` has closed: manage the position and look for entries."""
        price = float(cols["close"][i])
        atr = float(cols["atr"][i])
        self.last_atr = atr if atr == atr else self.last_atr  # NaN-safe
        self._apply_funding(now)
        self.risk.update(self.equity(price), self._utc(now).to_pydatetime())
        self._check_halt(now)

        if self.pending is not None:
            self.pending.bars_left -= 1
            if self.pending.bars_left <= 0:
                self._cancel_pending(now, "limit entry expired")

        if self.session_check(price, now):
            self._save_state()
            return "exit"

        decision = self.strategy.decide_at(cols, i, self.position)
        action = "hold"
        pos = self.position

        if pos is not None:
            if decision.action == EXIT:
                self._close(price, now, decision.reason)
                action = "exit"
            else:
                if decision.action == ADAPT:
                    pos.regime, pos.take_profit = TREND, None
                    log.info("position adapted to trend mode")
                    action = "adapt"
                elif pos.regime == RANGE and decision.take_profit is not None:
                    pos.take_profit = float(decision.take_profit)  # follow the moving middle band
                self._move_stop(pos)
        elif self.pending is not None:
            pass  # waiting for the limit entry
        elif self.cooldown > 0:
            self.cooldown -= 1
        elif decision.action == ENTER:
            if self.entry_gate is None or self.entry_gate(self, decision, cols, i):
                if self.try_enter(decision, price, now):
                    action = "enter"

        self._save_state()
        return action

    def try_enter(self, decision, price: float, now) -> bool:
        """Open a position for an ENTER decision if the account-level rules allow it."""
        if self.position is not None or self.pending is not None:
            return False
        ok, why = self.risk.can_open()
        if ok:
            ok, why = self._entry_window(now)
        if not ok:
            log.info("%s entry skipped: %s", self.symbol, why)
            return False
        if self._open(price, decision, now):
            self.trades_today += 1
            self._save_state()
            return True
        return False

    def _move_stop(self, pos: Position) -> None:
        """Ratchet the stop (never loosen it): break-even at N x R, ATR trail without a target."""
        s = _sign(pos.side)
        candidates = []
        be_r = float(self.cfg["strategy"].get("breakeven_r", 0) or 0)
        if be_r > 0 and pos.risk_dist > 0 and (pos.best - pos.entry_price) * s >= be_r * pos.risk_dist:
            fee_buffer = pos.entry_price * 2 * self.cfg["paper"]["taker_fee"]
            candidates.append(pos.entry_price + s * fee_buffer)
        if pos.take_profit is None and self.last_atr:  # riding a trend: trail it
            candidates.append(pos.best - s * self.cfg["strategy"]["trail_atr_mult"] * self.last_atr)
        best = max(candidates, key=lambda c: c * s, default=None)
        if best is not None and (best - pos.stop_price) * s > 0:
            pos.stop_price = best
            self._sync_exchange_stop()

    def _check_halt(self, now=None) -> None:
        ok, why = self.risk.can_open()
        if not ok and not self._halted:
            self.notify(f"⛔ 暫停開新倉: {why}")
            if self.risk.drawdown_halt and self.halted_at is None:
                self.halted_at = self._ts(now)
        self._halted = not ok

    # --------------------------------------------------------- close helper
    def _close(self, price: float, now, reason: str) -> None:
        pos = self.position
        try:
            fill = self.broker.market_close(pos.side, pos.amount, price, since=pos.opened_at)
        except Exception as exc:
            log.error("close order failed: %s (will retry)", exc)
            self.notify(f"⚠️ 平倉失敗，將重試: {exc}")
            return
        if fill is None:
            log.error("could not close position (order rejected); will retry")
            return
        self.broker.cancel_stop(pos.stop_order_id)
        amount = fill.amount if fill.amount > 0 else pos.amount
        exit_price = fill.price
        frac = min(1.0, amount / pos.amount)
        gross = _sign(pos.side) * (exit_price - pos.entry_price) * amount
        fees = pos.entry_fee * frac + fill.fee
        pnl = gross - fees - pos.funding * frac
        notional = pos.entry_price * amount
        trade = Trade(
            side=pos.side, opened_at=pos.opened_at, closed_at=self._ts(now), regime=pos.regime,
            entry_price=pos.entry_price, exit_price=exit_price, amount=amount, pnl=pnl,
            pnl_pct=pnl / notional * 100 if notional else 0.0, fees=fees,
            funding=pos.funding * frac, reason=reason,
            r_multiple=pnl / (pos.risk_dist * amount) if pos.risk_dist > 0 else 0.0,
            symbol=self.symbol,
        )
        self.trades.append(trade)
        self._journal(trade)
        icon = "✅" if pnl > 0 else "🔴"
        msg = (f"{icon} 平倉 {pos.side.upper()} {amount:.4f} {self.symbol} @ {exit_price:.2f}\n"
               f"損益 {pnl:+.2f} ({trade.pnl_pct:+.2f}%) | {reason}")
        log.info(msg.replace("\n", " | "))
        self.notify(msg)
        self.position = None
        self.cooldown = int(self.cfg["risk"].get("cooldown_bars", 0))
        self._save_state()
