import copy
import json

import numpy as np
import pandas as pd
import pytest

from cryptobot import indicators as ind
from cryptobot.__main__ import main
from cryptobot.backtest import run_backtest
from cryptobot.broker import PaperBroker
from cryptobot.config import DEFAULTS
from cryptobot.data import synthetic
from cryptobot.risk import RiskManager
from cryptobot.strategy import BUY, RANGE, TREND, Decision
from cryptobot.trader import Trader


@pytest.fixture
def cfg():
    return copy.deepcopy(DEFAULTS)


def test_indicators_basic():
    close = pd.Series(np.linspace(100, 200, 100))
    r = ind.rsi(close, 14)
    assert r.iloc[-1] == pytest.approx(100.0)  # only gains
    df = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close})
    assert ind.atr(df, 14).iloc[-1] > 0
    a = ind.adx(df, 14)
    assert a["adx"].iloc[-1] > 50 and a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]
    bb = ind.bollinger(close, 20, 2)
    assert (bb["bb_upper"].dropna() >= bb["bb_lower"].dropna()).all()


def test_position_size_respects_risk_and_caps():
    rm = RiskManager({"risk_per_trade": 0.01, "max_position_pct": 0.5,
                      "max_drawdown": 0.2, "daily_loss_limit": 0.05, "min_notional": 10})
    amt = rm.position_size(10000, 10000, 100, 98)  # risk $100 / $2 stop = 50 units ($5000)
    assert amt == pytest.approx(50)
    amt = rm.position_size(10000, 10000, 100, 99.9)  # tiny stop -> capped at 50% equity
    assert amt * 100 == pytest.approx(5000)
    assert rm.position_size(10000, 10000, 100, 101) == 0  # invalid stop


def test_drawdown_halt():
    from datetime import datetime
    rm = RiskManager({"risk_per_trade": 0.01, "max_position_pct": 0.5,
                      "max_drawdown": 0.2, "daily_loss_limit": 0.5, "min_notional": 10})
    rm.update(10000, datetime(2024, 1, 1))
    rm.update(7900, datetime(2024, 1, 2))
    assert rm.can_open()[0] is False


def _trader(cfg, tmp_path=None):
    broker = PaperBroker(10000, 0.001, 0.0)
    state = str(tmp_path / "state.json") if tmp_path else None
    return Trader(cfg, broker, state_path=state)


def test_stop_loss_and_take_profit(cfg):
    t = _trader(cfg)
    assert t._open(100.0, Decision(BUY, RANGE, stop_price=95.0, take_profit=110.0), None)
    assert not t.check_exits(96, 105)
    assert t.check_exits(94, 99, open_=97)  # stop hit
    assert t.trades[-1].exit_price == pytest.approx(95.0) and t.trades[-1].pnl < 0

    t._open(100.0, Decision(BUY, RANGE, stop_price=95.0, take_profit=110.0), None)
    assert t.check_exits(100, 112, open_=101)
    assert t.trades[-1].reason == "take profit" and t.trades[-1].pnl > 0


def test_gap_down_fills_at_open(cfg):
    t = _trader(cfg)
    t._open(100.0, Decision(BUY, TREND, stop_price=95.0), None)
    t.check_exits(80, 90, open_=88)
    assert t.trades[-1].exit_price == pytest.approx(88.0)


def test_state_persists(cfg, tmp_path):
    t = _trader(cfg, tmp_path)
    t._open(100.0, Decision(BUY, TREND, stop_price=95.0), None)
    t2 = _trader(cfg, tmp_path)
    assert t2.position is not None and t2.position.entry_price == pytest.approx(100.0)
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["position"]["regime"] == TREND


def test_no_lookahead(cfg):
    """Decisions on bar i must not change when future bars are appended."""
    candles = synthetic(bars=800, seed=3)
    t = _trader(cfg)
    full = t.strategy.analyze(candles)
    for i in range(400, 800, 37):
        part = t.strategy.analyze(candles.iloc[: i + 1])
        a = t.strategy.decide(part)
        b = t.strategy.decide(full.iloc[: i + 1])
        assert (a.action, a.regime) == (b.action, b.regime)


def test_backtest_runs(cfg):
    res = run_backtest(cfg, synthetic(bars=3000, seed=1))
    s = res.summary()
    assert s["trades"] > 0
    assert s["max_drawdown_pct"] <= 0
    # account never goes negative and never spends more than it has
    assert (res.equity > 0).all()


def test_live_requires_confirmation(capsys):
    assert main(["live"]) == 2


class FakeExchange:
    """Replays synthetic candles as if they were arriving live."""

    rateLimit = 0

    def __init__(self, candles):
        self.candles = candles
        self.i = 400

    def milliseconds(self):
        return int(self.candles.index[self.i].timestamp() * 1000) + 1

    def fetch_ticker(self, symbol):
        return {"last": float(self.candles["close"].iloc[self.i - 1])}

    def fetch_ohlcv(self, symbol, timeframe, limit=100, since=None):
        part = self.candles.iloc[max(0, self.i - limit + 1): self.i + 1]  # last row still forming
        return [[int(ts.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume]
                for ts, r in part.iterrows()]


def test_paper_runner_steps(cfg, tmp_path, monkeypatch):
    from cryptobot import runner

    candles = synthetic(bars=1200, seed=7)
    fake = FakeExchange(candles)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    r = runner.Runner(cfg, live=False)
    for i in range(400, 1200):
        fake.i = i
        r.step()
    assert r.trader.trades, "expected the paper bot to trade"
    assert (tmp_path / "logs").exists() and any((tmp_path / "logs").iterdir())
    assert r.trader.equity(float(candles["close"].iloc[-1])) > 0
