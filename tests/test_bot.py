import copy
import json

import numpy as np
import pandas as pd
import pytest

from cryptobot import indicators as ind
from cryptobot.__main__ import main
from cryptobot.backtest import make_paper_broker, run_backtest
from cryptobot.broker import PaperBroker
from cryptobot.config import DEFAULTS
from cryptobot.data import synthetic
from cryptobot.risk import RiskManager
from cryptobot.strategy import ENTER, LONG, RANGE, SHORT, TREND, Decision
from cryptobot.trader import Trader


@pytest.fixture
def cfg():
    c = copy.deepcopy(DEFAULTS)
    c["notify"]["telegram"] = False
    return c


@pytest.fixture
def market_cfg(cfg):
    """Market orders, no day-trade session: isolates the core position logic."""
    cfg["orders"]["entry_type"] = "market"
    cfg["daytrade"]["enabled"] = False
    cfg["paper"]["slippage"] = 0.0
    return cfg


def _trader(cfg, tmp_path=None):
    state = str(tmp_path / "state.json") if tmp_path else None
    return Trader(cfg, make_paper_broker(cfg), state_path=state)


def _enter(side, stop, tp=None, regime=TREND):
    return Decision(ENTER, regime, "test", side=side, stop_price=stop, take_profit=tp)


# ------------------------------------------------------------------ indicators
def test_indicators_basic():
    close = pd.Series(np.linspace(100, 200, 100))
    assert ind.rsi(close, 14).iloc[-1] == pytest.approx(100.0)  # only gains
    df = pd.DataFrame({"high": close + 1, "low": close - 1, "close": close})
    assert ind.atr(df, 14).iloc[-1] > 0
    a = ind.adx(df, 14)
    assert a["adx"].iloc[-1] > 50 and a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]
    bb = ind.bollinger(close, 20, 2)
    assert (bb["bb_upper"].dropna() >= bb["bb_lower"].dropna()).all()


# ------------------------------------------------------------------------ risk
def test_position_size_respects_risk_and_leverage():
    rm = RiskManager({"risk_per_trade": 0.02, "max_exposure": 5, "max_drawdown": 0.2,
                      "daily_loss_limit": 0.06, "min_notional": 20})
    # risk $6 of $300 with a $2 stop -> 3 units ($300 notional)
    assert rm.position_size(300, 300, 100, 98, leverage=5) == pytest.approx(3)
    assert rm.position_size(300, 300, 100, 102, leverage=5) == pytest.approx(3)  # short side
    # tiny stop -> capped at 5x equity (minus a 5% free-margin buffer)
    assert rm.position_size(300, 300, 100, 99.99, leverage=5) * 100 == pytest.approx(1425)
    # spot: leverage 1 caps at 1x equity
    assert rm.position_size(300, 300, 100, 99.99, leverage=1) * 100 == pytest.approx(285)
    assert rm.position_size(300, 300, 100, 100, leverage=5) == 0


def test_drawdown_halt():
    from datetime import datetime
    rm = RiskManager({"risk_per_trade": 0.01, "max_exposure": 1, "max_drawdown": 0.2,
                      "daily_loss_limit": 0.5, "min_notional": 10})
    rm.update(10000, datetime(2024, 1, 1))
    rm.update(7900, datetime(2024, 1, 2))
    assert rm.can_open()[0] is False


# ----------------------------------------------------------------- paper broker
def test_paper_broker_short_pnl_and_fees():
    b = PaperBroker(1000, maker_fee=0.0, taker_fee=0.001, slippage=0.0, leverage=5)
    b.market_open(SHORT, 10, 100)  # $1000 notional, $1 fee
    assert b.equity(90) == pytest.approx(1000 - 1 + 100)
    b.market_close(SHORT, 10, 90)  # +$100, $0.9 fee
    assert b.wallet == pytest.approx(1000 - 1 + 100 - 0.9)
    assert b.position_amount() == 0


# --------------------------------------------------------------- exits / pnl
def test_long_stop_and_take_profit(market_cfg):
    t = _trader(market_cfg)
    assert t._open(100.0, _enter(LONG, 98.0, 104.0, RANGE), None)
    assert not t.check_exits(99, 103, intrabar=True)
    assert t.check_exits(97, 99, open_=99.5, intrabar=True)
    assert t.trades[-1].exit_price == pytest.approx(98.0) and t.trades[-1].pnl < 0

    t.cooldown = 0
    t._open(100.0, _enter(LONG, 98.0, 104.0, RANGE), None)
    assert t.check_exits(100, 105, open_=101, intrabar=True)
    assert t.trades[-1].reason == "take profit" and t.trades[-1].pnl > 0


def test_short_stop_take_profit_and_gap(market_cfg):
    t = _trader(market_cfg)
    t._open(100.0, _enter(SHORT, 102.0, 96.0, RANGE), None)
    assert t.position.side == SHORT and t.position.stop_price == pytest.approx(102.0)
    assert t.check_exits(95, 99, open_=99, intrabar=True)  # target below
    assert t.trades[-1].exit_price == pytest.approx(96.0) and t.trades[-1].pnl > 0

    t._open(100.0, _enter(SHORT, 102.0), None)
    t.check_exits(104, 110, open_=105, intrabar=True)  # gapped above the stop
    assert t.trades[-1].exit_price == pytest.approx(105.0) and t.trades[-1].pnl < 0


def test_short_trailing_stop_moves_down(market_cfg):
    t = _trader(market_cfg)
    t._open(100.0, _enter(SHORT, 103.0), None)
    t.last_atr = 1.0
    t.check_exits(90, 95, intrabar=True)  # price fell, best = 90
    assert t.position.best == pytest.approx(90)
    trail = t.position.best + market_cfg["strategy"]["trail_atr_mult"] * 1.0
    # mimic the candle-close trailing update
    s = -1
    if (trail - t.position.stop_price) * s > 0:
        t.position.stop_price = trail
    assert t.position.stop_price == pytest.approx(trail) and trail < 100


# ------------------------------------------------------------- limit entries
def test_limit_entry_fills_only_when_touched(cfg):
    cfg["daytrade"]["enabled"] = False
    t = _trader(cfg)
    assert t._open(100.0, _enter(LONG, 98.0), None)
    assert t.pending is not None and t.position is None
    assert not t.check_pending(100.5, 101)  # never traded down to 100
    assert t.check_pending(99.8, 101)
    assert t.position.entry_price == pytest.approx(100.0)
    assert t.position.entry_fee == pytest.approx(t.position.amount * 100 * cfg["paper"]["maker_fee"])


# ------------------------------------------------------------------ day trade
def test_daytrade_session_rolls_at_0745_taipei(market_cfg):
    market_cfg["daytrade"]["enabled"] = True
    t = _trader(market_cfg)
    opened = pd.Timestamp("2024-03-01T02:00:00Z")  # 10:00 Taipei
    assert t._entry_window(opened)[0]
    t._open(100.0, _enter(LONG, 98.0), opened)
    # 03:00 Taipei next calendar day is still the same trading day
    assert not t.session_check(101.0, pd.Timestamp("2024-03-01T19:00:00Z"))
    # 06:50 Taipei: inside the last hour -> no new entries
    assert not t._entry_window(pd.Timestamp("2024-03-01T22:50:00Z"))[0]
    # 07:46 Taipei: new trading day -> forced flat
    assert t.session_check(101.0, pd.Timestamp("2024-03-01T23:46:00Z"))
    assert t.position is None and t.trades[-1].reason == "day-trade session close"


def test_funding_charged_when_crossing_funding_time(market_cfg):
    t = _trader(market_cfg)
    t._open(100.0, _enter(LONG, 98.0), pd.Timestamp("2024-03-01T07:00:00Z"))
    t._apply_funding(pd.Timestamp("2024-03-01T09:00:00Z"))  # crosses 08:00 UTC
    expected = t.position.amount * 100 * market_cfg["futures"]["funding_rate"]
    assert t.position.funding == pytest.approx(expected)
    t._apply_funding(pd.Timestamp("2024-03-01T10:00:00Z"))  # nothing new
    assert t.position.funding == pytest.approx(expected)


# ------------------------------------------------------------------- state
def test_state_persists(market_cfg, tmp_path):
    t = _trader(market_cfg, tmp_path)
    t._open(100.0, _enter(SHORT, 102.0), None)
    t2 = _trader(market_cfg, tmp_path)
    assert t2.position is not None and t2.position.side == SHORT
    assert t2.broker.position_amount() < 0  # paper account restored too
    data = json.loads((tmp_path / "state.json").read_text())
    assert data["position"]["regime"] == TREND


# ------------------------------------------------------------ strategy / e2e
def test_no_lookahead(cfg):
    """Decisions on bar i must not change when future bars are appended."""
    candles = synthetic(bars=800, seed=3)
    t = _trader(cfg)
    full = t.strategy.analyze(candles)
    for i in range(400, 800, 37):
        a = t.strategy.decide(t.strategy.analyze(candles.iloc[: i + 1]))
        b = t.strategy.decide(full.iloc[: i + 1])
        assert (a.action, a.regime, a.side) == (b.action, b.regime, b.side)


def test_backtest_trades_both_sides(cfg):
    res = run_backtest(cfg, synthetic(bars=4000, timeframe="15m", seed=1))
    s = res.summary()
    sides = {t.side for t in res.trades}
    assert s["trades"] > 0 and sides == {LONG, SHORT}
    assert s["max_drawdown_pct"] <= 0 and (res.equity > 0).all()
    # day-trade mode: nothing is ever held across a 07:45 Taipei boundary
    tr = Trader(cfg, make_paper_broker(cfg))
    for t in res.trades:
        assert tr._session_of(t.opened_at) == tr._session_of(pd.Timestamp(t.closed_at) - pd.Timedelta(seconds=1))


def test_spot_mode_never_shorts(cfg):
    cfg["market"] = "spot"
    cfg["symbol"] = "ETH/USDT"
    res = run_backtest(cfg, synthetic(bars=3000, timeframe="15m", seed=1))
    assert res.trades and all(t.side == LONG for t in res.trades)


def test_live_requires_confirmation():
    assert main(["live"]) == 2


def test_optimize_runs(cfg):
    from cryptobot.optimize import optimize
    grid = {"strategy.stop_atr_mult": [2.0, 3.0]}
    res = optimize(cfg, {"15m": synthetic(bars=2500, timeframe="15m", seed=2)}, grid=grid, workers=1)
    assert len(res) == 2 and {"in", "out"} <= set(res[0])


# -------------------------------------------------------------- live runner
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

    candles = synthetic(bars=1400, timeframe="15m", seed=7)
    fake = FakeExchange(candles)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    r = runner.Runner(cfg, live=False)
    for i in range(400, 1400):
        fake.i = i
        r.step()
    assert r.trader.trades, "expected the paper bot to trade"
    assert any((tmp_path / "logs").iterdir())
    assert r.trader.equity(float(candles["close"].iloc[-1])) > 0


def test_optimizer_only_recommends_robust_results():
    from cryptobot.optimize import robust

    def res(ret_in, ret_out, n_in=30, n_out=15):
        return {"in": {"total_return_pct": ret_in, "trades": n_in},
                "out": {"total_return_pct": ret_out, "trades": n_out}}

    assert robust(res(5, 3))
    assert not robust(res(-6.7, 13.0))  # lucky out-of-sample only (seen on real ETH data)
    assert not robust(res(5, -1))
    assert not robust(res(5, 3, n_in=4))  # too few trades to trust
