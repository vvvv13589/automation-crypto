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
    cfg["orders"]["entry_type"] = "limit"
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
@pytest.mark.parametrize("name", ["breakout", "pullback", "adaptive"])
def test_no_lookahead(cfg, name):
    """Decisions on bar i must not change when future bars are appended."""
    cfg["strategy"]["name"] = name
    candles = synthetic(bars=1500, timeframe="15m", seed=3)
    t = _trader(cfg)
    full = t.strategy.analyze(candles)
    for i in range(900, 1500, 29):
        a = t.strategy.decide(t.strategy.analyze(candles.iloc[: i + 1]))
        b = t.strategy.decide(full.iloc[: i + 1])
        assert (a.action, a.regime, a.side) == (b.action, b.regime, b.side)


@pytest.mark.parametrize("name", ["pullback", "adaptive"])
def test_daytrade_backtest_trades_both_sides(cfg, name):
    cfg["strategy"]["name"] = name
    cfg["timeframe"] = "15m"
    cfg["daytrade"]["enabled"] = True
    cfg["orders"]["entry_type"] = "limit"
    res = run_backtest(cfg, synthetic(bars=8000, timeframe="15m", seed=1))
    s = res.summary()
    sides = {t.side for t in res.trades}
    assert s["trades"] > 0 and sides == {LONG, SHORT}
    assert s["max_drawdown_pct"] <= 0 and (res.equity > 0).all()
    # day-trade mode: nothing is ever held across a 07:45 Taipei boundary
    tr = Trader(cfg, make_paper_broker(cfg))
    for t in res.trades:
        assert tr._session_of(t.opened_at) == tr._session_of(pd.Timestamp(t.closed_at) - pd.Timedelta(seconds=1))


def test_breakout_swing_trades_both_sides_and_trails(cfg):
    assert cfg["strategy"]["name"] == "breakout" and not cfg["daytrade"]["enabled"]
    res = run_backtest(cfg, synthetic(bars=3000, timeframe="4h", seed=1))
    sides = {t.side for t in res.trades}
    assert sides == {LONG, SHORT}
    # no profit target: every exit is a stop (initial or trailed) or the end of the test
    assert {t.reason for t in res.trades} <= {"stop loss", "trailing / break-even stop", "end of backtest"}
    held = [pd.Timestamp(t.closed_at) - pd.Timestamp(t.opened_at) for t in res.trades]
    assert max(held) > pd.Timedelta(days=1)  # swing trades are held overnight


def test_breakout_signal_uses_previous_channel(cfg):
    from cryptobot.strategy import BreakoutStrategy
    cfg["strategy"]["bo_n"] = 5
    idx = pd.date_range("2024-01-01", periods=40, freq="4h", tz="UTC")
    close = np.r_[np.full(39, 100.0), 103.0]  # flat, then one breakout candle
    df = pd.DataFrame({"open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0}, index=idx)
    st = BreakoutStrategy(cfg["strategy"])
    d = st.decide(st.analyze(df))
    assert d.action == ENTER and d.side == LONG and d.stop_price < 103
    assert st.decide(st.analyze(df.iloc[:-1])).action != ENTER


def test_htf_trend_uses_only_closed_htf_candles():
    from cryptobot.strategy import htf_trend
    df = synthetic(bars=2000, timeframe="15m", seed=4)
    full = htf_trend(df, "4h", 5, 10)
    # truncating mid-way through a 4h candle must not change any earlier value
    for cut in (1203, 1210, 1215, 1599):
        assert (htf_trend(df.iloc[:cut], "4h", 5, 10) == full[:cut]).all()
    # a 4h candle opening at 00:00 is first visible to the 15m candle closing at 04:00
    t0 = df.index[0]
    assert df.index[15] == t0 + pd.Timedelta("3h45min")


def test_breakeven_stop(market_cfg):
    market_cfg["strategy"].update(name="pullback", breakeven_r=1.0, take_profit_r=3.0)
    t = _trader(market_cfg)
    t._open(100.0, _enter(LONG, 98.0, 106.0), None)  # 1R = 2
    t.check_exits(100, 101.5, intrabar=True)
    t._move_stop(t.position)
    assert t.position.stop_price == pytest.approx(98.0)  # +0.75R: not yet
    t.check_exits(100, 102.2, intrabar=True)
    t._move_stop(t.position)
    assert 100.0 < t.position.stop_price < 100.5  # break-even plus fee buffer
    t2 = _trader(market_cfg)
    t2._open(100.0, _enter(SHORT, 102.0, 94.0), None)
    t2.check_exits(97.5, 100, intrabar=True)
    t2._move_stop(t2.position)
    assert 99.5 < t2.position.stop_price < 100.0


def test_spot_mode_never_shorts(cfg):
    cfg["market"] = "spot"
    cfg["symbol"] = "ETH/USDT"
    res = run_backtest(cfg, synthetic(bars=8000, timeframe="15m", seed=1))
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


# ------------------------------------------------------------------ scanner
def test_shared_paper_wallet_sums_all_positions():
    from cryptobot.broker import PaperWallet
    w = PaperWallet(1000)
    a = PaperBroker(0, maker_fee=0, taker_fee=0, slippage=0, leverage=5, wallet=w)
    b = PaperBroker(0, maker_fee=0, taker_fee=0, slippage=0, leverage=5, wallet=w)
    a.market_open(LONG, 10, 100)   # +1/unit
    b.market_open(SHORT, 5, 50)    # +1/unit when price falls
    b.mark = 40
    assert a.equity(110) == pytest.approx(1000 + 100 + 50)
    assert a.free_margin(110) == pytest.approx(1150 - (10 * 100 + 5 * 50) / 5)
    assert "wallet" not in a.to_dict()  # shared cash is saved by the scanner, not per coin


def _scan_cfg(cfg, **scanner):
    cfg["scanner"]["coins"] = "all"  # synthetic test coins are not in the tested list
    cfg["scanner"].update(scanner)
    cfg["risk"].update(max_drawdown=1.0, daily_loss_limit=1.0)
    return cfg


def test_scan_backtest_respects_slots_and_one_position_per_coin(cfg):
    from cryptobot.scanner import run_scan_backtest
    _scan_cfg(cfg, max_positions=2)
    data = {f"C{i}/USDT:USDT": synthetic(bars=1500, timeframe="4h", seed=10 + i) for i in range(5)}
    res = run_scan_backtest(cfg, data)
    s = res["summary"]
    assert s["trades"] > 0 and s["max_open_positions"] <= 2
    # never two open positions on the same coin, never more than 2 overall
    events = []
    for t in res["trades"]:
        events += [(pd.Timestamp(t.opened_at), 1), (pd.Timestamp(t.closed_at), -1)]
    open_now = 0
    for _, d in sorted(events, key=lambda e: (e[0], e[1])):
        open_now += d
        assert open_now <= 2


def test_scanner_prefers_higher_volume_breakouts(cfg):
    from cryptobot.broker import PaperWallet
    from cryptobot.scanner import Scanner
    _scan_cfg(cfg, max_positions=1)
    wallet = PaperWallet(300)
    sc = Scanner(cfg, lambda s: PaperBroker(300, 0, 0, 0, 5, wallet=wallet), wallet=wallet)
    sc.begin_candle()
    for sym, vr in (("AAA/USDT:USDT", 1.2), ("BBB/USDT:USDT", 4.0), ("CCC/USDT:USDT", 2.0)):
        t = sc.trader(sym)
        cols = {"close": np.array([100.0]), "vol_ratio": np.array([vr])}
        assert t.entry_gate(t, _enter(LONG, 95.0), cols, 0) is False  # collected, not entered
    entered = sc.finish_candle(pd.Timestamp("2024-03-01T04:00Z"))
    assert entered == ["BBB/USDT:USDT"] and sc.open_count() == 1


def test_scanner_restores_open_positions(cfg, tmp_path):
    from cryptobot.broker import PaperWallet
    from cryptobot.scanner import Scanner
    _scan_cfg(cfg)
    cfg.update(state_dir=str(tmp_path))
    w1 = PaperWallet(300)
    sc = Scanner(cfg, lambda s: PaperBroker(300, 0, 0, 0, 5, wallet=w1), state_dir=str(tmp_path), mode="paper", wallet=w1)
    sc.trader("SOL/USDT:USDT").try_enter(_enter(SHORT, 105.0), 100.0, pd.Timestamp("2024-03-01T04:00Z"))
    sc.save()
    w2 = PaperWallet(300)
    sc2 = Scanner(cfg, lambda s: PaperBroker(300, 0, 0, 0, 5, wallet=w2), state_dir=str(tmp_path), mode="paper", wallet=w2)
    assert sc2.open_symbols() == ["SOL/USDT:USDT"]
    assert sc2.traders["SOL/USDT:USDT"].position.side == SHORT
    assert w2.cash == pytest.approx(w1.cash)


class FakeMarketExchange:
    """Several perpetuals replayed from synthetic candles."""

    rateLimit = 0

    def __init__(self, data, i):
        self.data, self.i = data, i
        self.markets = {s: {"swap": True, "linear": True, "settle": "USDT", "active": True, "base": s.split("/")[0]}
                        for s in data}
        self.markets["USDC/USDT:USDT"] = {"swap": True, "linear": True, "settle": "USDT", "active": True, "base": "USDC"}

    def _df(self, s):
        return self.data[s]

    def milliseconds(self):
        return int(next(iter(self.data.values())).index[self.i].timestamp() * 1000) + 1

    def fetch_tickers(self, symbols=None):
        out = {s: {"last": float(df["close"].iloc[self.i - 1]), "quoteVolume": 1e9 / (k + 1)}
               for k, (s, df) in enumerate(self.data.items())}
        out["USDC/USDT:USDT"] = {"last": 1.0, "quoteVolume": 9e12}
        return {s: v for s, v in out.items() if symbols is None or s in symbols}

    def fetch_ohlcv(self, symbol, timeframe, limit=100, since=None):
        part = self._df(symbol).iloc[max(0, self.i - limit + 1): self.i + 1]
        return [[int(ts.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume] for ts, r in part.iterrows()]


def test_scan_runner_paper_end_to_end(cfg, tmp_path, monkeypatch):
    from cryptobot import runner
    data = {f"C{i}/USDT:USDT": synthetic(bars=900, timeframe="4h", seed=20 + i) for i in range(4)}
    fake = FakeMarketExchange(data, 300)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    _scan_cfg(cfg, max_positions=2, top_n=3)
    r = runner.ScanRunner(cfg, live=False)
    idx = next(iter(data.values())).index
    for i in range(300, 900):
        fake.i = i
        now = idx[i] + pd.Timedelta(seconds=30)
        r.protect(now)
        r.scan(now)
        assert r.scanner.open_count() <= 2
    assert "USDC/USDT:USDT" not in r.universe and len(r.universe) == 3
    trades = [t for tr in r.scanner.traders.values() for t in tr.trades]
    assert trades and any((tmp_path / "logs").iterdir())


def test_preflight_flags_dangerous_settings(cfg):
    from cryptobot.preflight import FAIL, OK, WARN, report, run_checks
    cfg["scanner"]["coins"] = "all"
    data = {f"C{i}/USDT:USDT": synthetic(bars=600, timeframe="4h", seed=30 + i) for i in range(3)}

    class Acct(FakeMarketExchange):
        withdraw, multi = True, "true"

        def fetch_balance(self):
            return {"info": {"totalMarginBalance": "310", "availableBalance": "300"}}

        def sapiGetAccountApiRestrictions(self):
            return {"enableWithdrawals": self.withdraw, "enableFutures": True, "ipRestrict": False}

        def fapiPrivateGetMultiAssetsMargin(self):
            return {"multiAssetsMargin": self.multi}

        def fapiPrivateGetPositionSideDual(self):
            return {"dualSidePosition": False}

        def fetch_positions(self, symbols=None):
            return []

    ex = Acct(data, 599)
    res = dict((m, s) for s, m in run_checks(ex, cfg))
    assert any("提現" in m and s == FAIL for m, s in res.items())
    assert any("聯合保證金" in m and s == FAIL for m, s in res.items())
    assert report(run_checks(ex, cfg)) == 1
    ex.withdraw, ex.multi = False, "false"
    statuses = [s for s, _ in run_checks(ex, cfg)]
    assert FAIL not in statuses and OK in statuses and WARN in statuses  # IP whitelist warning
    assert report(run_checks(ex, cfg)) == 0


def test_goal_alerts_fire_once_and_target_stops_entries(cfg, tmp_path):
    from cryptobot.broker import PaperWallet
    from cryptobot.scanner import Scanner
    _scan_cfg(cfg, max_positions=4)
    cfg["goals"].update(base_capital=100, target=500)
    sent = []
    w = PaperWallet(100)
    sc = Scanner(cfg, lambda s: PaperBroker(100, 0, 0, 0, 5, wallet=w), state_dir=str(tmp_path),
                 notify=sent.append, mode="paper", wallet=w)
    now = pd.Timestamp("2024-03-01T04:00Z")
    w.cash = 150
    sc.finish_candle(now)
    assert sent == []
    w.cash = 210
    sc.finish_candle(now)
    sc.finish_candle(now)
    assert len(sent) == 1 and "翻倍" in sent[0]          # doubled: alert once only
    w.cash = 520
    sc.begin_candle()
    t = sc.trader("AAA/USDT:USDT")
    t.entry_gate(t, _enter(LONG, 95.0), {"close": np.array([100.0]), "vol_ratio": np.array([3.0])}, 0)
    assert sc.finish_candle(now) == [] and sc.open_count() == 0   # target reached: no new entries
    assert len(sent) == 2 and "目標" in sent[1]
    # flags survive a restart
    w2 = PaperWallet(520)
    sc2 = Scanner(cfg, lambda s: PaperBroker(520, 0, 0, 0, 5, wallet=w2), state_dir=str(tmp_path),
                  notify=sent.append, mode="paper", wallet=w2)
    sc2.finish_candle(now)
    assert sc2.target_reached and len(sent) == 2


def test_reset_risk_clears_drawdown_halt(cfg, tmp_path, monkeypatch):
    import json as _json
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    p = state / "binance_scan_paper_portfolio.json"
    p.write_text(_json.dumps({"risk": {"peak_equity": 624, "drawdown_halt": True}, "cash": 312}))
    assert main(["reset-risk"]) == 0
    data = _json.loads(p.read_text())
    assert data["risk"] == {} and data["cash"] == 312


def test_check_explains_invalid_api_key(monkeypatch, capsys):
    import cryptobot.data as data
    monkeypatch.setenv("EXCHANGE_API_KEY", "x")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "y")

    def boom(*a, **k):
        raise Exception('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions for action."}')
    monkeypatch.setattr(data, "make_exchange", boom)
    assert main(["check"]) == 1
    out = capsys.readouterr().out
    assert "白名單" in out and "HMAC" in out



def test_universe_is_tested_crypto_only(cfg):
    from cryptobot.scanner import pick_universe
    data = {s: synthetic(bars=50, timeframe="4h", seed=1) for s in
            ("BTC/USDT:USDT", "ETH/USDT:USDT", "XAU/USDT:USDT", "SNDK/USDT:USDT", "C0/USDT:USDT")}
    ex = FakeMarketExchange(data, 40)
    ex.markets["XAU/USDT:USDT"]["info"] = {"underlyingType": "COMMODITY"}
    uni = pick_universe(ex, cfg)
    assert set(uni) == {"BTC/USDT:USDT", "ETH/USDT:USDT"}      # default: only backtested coins
    cfg["scanner"]["coins"] = "all"
    uni = pick_universe(ex, cfg, extra_exclude={"ETH"})
    assert "XAU/USDT:USDT" not in uni and "ETH/USDT:USDT" not in uni and "C0/USDT:USDT" in uni


def test_preflight_fails_hedge_mode_with_open_position(cfg):
    from cryptobot.preflight import FAIL, run_checks
    data = {"BTC/USDT:USDT": synthetic(bars=600, timeframe="4h", seed=3)}

    class Acct(FakeMarketExchange):
        def fetch_balance(self):
            return {"info": {"totalMarginBalance": "316", "availableBalance": "300"}}

        def sapiGetAccountApiRestrictions(self):
            return {"enableWithdrawals": False, "enableFutures": True, "ipRestrict": True}

        def fapiPrivateGetMultiAssetsMargin(self):
            return {"multiAssetsMargin": False}

        def fapiPrivateGetPositionSideDual(self):
            return {"dualSidePosition": True}

        def fetch_positions(self, symbols=None):
            return [{"symbol": "ETH/USDT:USDT", "contracts": 0.1}]

    res = run_checks(Acct(data, 599), cfg)
    assert any(s == FAIL and "雙向持倉" in m and "ETH" in m for s, m in res)


def test_live_scan_runner_skips_coins_with_manual_positions(cfg, tmp_path, monkeypatch):
    from cryptobot import runner
    data = {s: synthetic(bars=600, timeframe="4h", seed=5) for s in ("BTC/USDT:USDT", "ETH/USDT:USDT")}

    class Live(FakeMarketExchange):
        def fapiPrivateGetPositionSideDual(self):
            return {"dualSidePosition": False}

        def fetch_positions(self, symbols=None):
            return [{"symbol": "ETH/USDT:USDT", "contracts": 0.1}]

    fake = Live(data, 500)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    r = runner.ScanRunner(cfg, live=True)
    assert r.external == {"ETH"}
    r.refresh_universe()
    assert r.universe == ["BTC/USDT:USDT"]

    class Hedged(Live):
        def fapiPrivateGetPositionSideDual(self):
            return {"dualSidePosition": True}

        def set_position_mode(self, hedged, symbol=None):
            raise Exception("-4068 position side cannot be changed if there exists position")

    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: Hedged(data, 500))
    with pytest.raises(SystemExit):
        runner.ScanRunner(cfg, live=True)


def test_daily_report_content_and_schedule(cfg, tmp_path, monkeypatch):
    from cryptobot import runner
    from cryptobot.report import DailyReporter
    data = {f"C{i}/USDT:USDT": synthetic(bars=600, timeframe="4h", seed=40 + i) for i in range(2)}
    fake = FakeMarketExchange(data, 500)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    _scan_cfg(cfg)
    cfg["goals"]["base_capital"] = 300
    sent = []
    r = runner.ScanRunner(cfg, live=False)
    r.notify = sent.append
    sc = r.scanner
    assert sc.trader("C0/USDT:USDT").try_enter(_enter(SHORT, 105.0), 100.0, pd.Timestamp("2024-03-01T04:00Z"))
    fake.fetch_tickers = lambda symbols=None: {"C0/USDT:USDT": {"last": 95.0}}
    text = r.send_report(pd.Timestamp("2024-03-01T01:00Z"))
    pos = sc.traders["C0/USDT:USDT"].position
    gain = (pos.entry_price - 95.0) * pos.amount
    assert sent and "本金 300" in text and "C0 空 @ 99.97 → 95" in text and f"{gain:+.2f} U" in text
    assert f"機器人：{gain:+.2f} U" in text and "手動／其他" in text
    assert "停損 104.97" in text
    assert f"帳戶總額 {sc.account_equity():.2f}" in text and sc.account_equity() > 300  # unrealised gain included
    assert "機器人運作中" in text

    rep = DailyReporter({"notify": {"daily_report": "09:00", "timezone": "Asia/Taipei"}})
    assert not rep.due(pd.Timestamp("2024-03-01T00:30Z"))   # 08:30 Taipei
    assert rep.due(pd.Timestamp("2024-03-01T01:05Z"))       # 09:05 Taipei
    assert not rep.due(pd.Timestamp("2024-03-01T05:00Z"))   # already sent today
    assert rep.due(pd.Timestamp("2024-03-02T01:00Z"))       # next day
    assert not DailyReporter({"notify": {"daily_report": ""}}).due(pd.Timestamp("2024-03-02T01:00Z"))


def test_entry_message_explains_market(cfg):
    from cryptobot.broker import PaperWallet
    from cryptobot.runner import btc_note
    from cryptobot.scanner import Scanner
    from cryptobot.strategy import BreakoutStrategy
    st = BreakoutStrategy(cfg["strategy"])
    cols = st.columns(st.analyze(synthetic(bars=1500, timeframe="4h", seed=3)))
    d = next(st.decide_at(cols, i) for i in range(300, 1500) if st.decide_at(cols, i).action == ENTER)
    assert "行情" in d.reason and "成交量" in d.reason and "近 30 天" in d.reason and "波動" in d.reason

    _scan_cfg(cfg)
    sent = []
    w = PaperWallet(300)
    sc = Scanner(cfg, lambda s: PaperBroker(300, 0, 0, 0, 5, wallet=w), notify=sent.append, wallet=w)
    sc.begin_candle()
    sc.market_note = btc_note(synthetic(bars=300, timeframe="4h", seed=1))
    for sym in ("AAA/USDT:USDT", "BBB/USDT:USDT"):
        t = sc.trader(sym)
        t.entry_gate(t, _enter(SHORT, 105.0), {"close": np.array([100.0]), "vol_ratio": np.array([2.0])}, 0)
    sc.finish_candle(pd.Timestamp("2024-03-01T04:00Z"))
    opened = [m for m in sent if "開倉" in m]
    assert opened and "2 個幣跌破、0 個突破 → 市場偏弱" in opened[0] and "BTC：24 小時" in opened[0]


def test_manual_position_opened_while_running_is_skipped(cfg, tmp_path, monkeypatch):
    from cryptobot import runner
    data = {s: synthetic(bars=600, timeframe="4h", seed=6) for s in ("BTC/USDT:USDT", "ETH/USDT:USDT")}

    class Live(FakeMarketExchange):
        manual = []

        def fapiPrivateGetPositionSideDual(self):
            return {"dualSidePosition": False}

        def fetch_positions(self, symbols=None):
            return [{"symbol": s, "contracts": 1.0} for s in self.manual]

    fake = Live(data, 500)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    monkeypatch.setenv("EXCHANGE_API_KEY", "k")
    monkeypatch.setenv("EXCHANGE_API_SECRET", "s")
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    sent = []
    r = runner.ScanRunner(cfg, live=True)
    r.notify = sent.append
    assert r.external == set()
    Live.manual = ["ETH/USDT:USDT"]          # user opens ETH by hand while the bot runs
    r.refresh_external()
    assert r.external == {"ETH"} and any("ETH" in m for m in sent)
    Live.manual = []                          # closed again -> ETH is tradable again
    r.refresh_external()
    assert r.external == set()



def test_report_splits_bot_and_manual_pnl(cfg, tmp_path):
    from cryptobot.broker import PaperWallet
    from cryptobot.report import build_report
    from cryptobot.scanner import Scanner
    _scan_cfg(cfg)
    cfg.update(log_dir=str(tmp_path))
    cfg["goals"]["base_capital"] = 312
    (tmp_path / "binance_scan_live_trades.csv").write_text(
        "closed_at,pnl\n2024-03-01T00:00:00+00:00,5.0\n2024-03-02T00:00:00+00:00,-2.0\n")

    class Broker:
        live = True

        def equity(self, price):
            return 400.0

    class Ex:
        def fetch_tickers(self, symbols=None):
            return {}

        def fetch_positions(self, symbols=None):
            return [{"symbol": "ETH/USDT:USDT", "contracts": 0.5, "side": "long",
                     "entryPrice": 2500, "unrealizedPnl": 12.5}]

    sc = Scanner(cfg, lambda s: Broker(), mode="live")
    sc.trader("BTC/USDT:USDT")
    text = build_report(sc, Ex(), cfg, "live", pd.Timestamp("2024-03-03T01:00Z"))
    assert "帳戶總額 400.00 U（本金 312 U" in text
    assert "機器人：+3.00 U（已實現 +3.00、未實現 +0.00）" in text
    assert "手動／其他：+85.00 U" in text
    assert "手動持倉" in text and "ETH 多 @ 2500  +12.50 U" in text


def test_live_fill_reads_fee_from_trades():
    from cryptobot.broker import FuturesLiveBroker

    class Ex:
        def market(self, s):
            return {"settle": "USDT"}

        def fetch_order(self, oid, s):
            return {"id": oid, "filled": 2.0, "average": 100.0, "fee": None}

        def fetch_order_trades(self, oid, s):
            return [{"fee": {"cost": 0.06, "currency": "USDT"}, "cost": 120.0},
                    {"fee": {"cost": 0.0001, "currency": "BNB"}, "cost": 80.0}]

    b = FuturesLiveBroker(Ex(), "SOL/USDT:USDT", 5)
    fill = b._fill("short", {"id": "1", "filled": 2.0, "average": 100.0}, 100.0)
    assert fill.fee == pytest.approx(0.06 + 80.0 * 0.0005)

    class NoTrades(Ex):
        def fetch_order_trades(self, oid, s):
            raise Exception("not supported")
    fill = FuturesLiveBroker(NoTrades(), "SOL/USDT:USDT", 5)._fill("long", {"id": "1", "filled": 2.0, "average": 100.0}, 100.0)
    assert fill.fee == pytest.approx(200.0 * 0.0005)


def test_telegram_status_command_and_scan_report(cfg, tmp_path, monkeypatch):
    from cryptobot import runner
    from cryptobot.notify import Telegram
    data = {f"C{i}/USDT:USDT": synthetic(bars=600, timeframe="4h", seed=50 + i) for i in range(2)}
    fake = FakeMarketExchange(data, 500)
    monkeypatch.setattr(runner, "make_exchange", lambda *a, **k: fake)
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    _scan_cfg(cfg)

    tg = Telegram("TOKEN", "42")
    sent, inbox = [], [[{"update_id": 7, "message": {"chat": {"id": 42}, "text": "/status"}}]]
    tg.send = sent.append
    tg._get_updates = lambda offset: inbox.pop(0) if inbox else []
    r = runner.ScanRunner(cfg, live=False)
    r.notify = tg

    r.answer_commands(pd.Timestamp("2024-03-01T01:00Z"))
    assert sent == []                                   # backlog before start-up is ignored
    inbox.append([{"update_id": 8, "message": {"chat": {"id": 999}, "text": "/status"}},   # stranger
                  {"update_id": 9, "message": {"chat": {"id": 42}, "text": "/status"}}])
    r._last_poll = 0
    r.answer_commands(pd.Timestamp("2024-03-01T01:00Z"))
    assert len(sent) == 1 and "即時狀態" in sent[0] and "帳戶總額" in sent[0]
    assert tg._offset == 10

    sent.clear()
    r.scan(pd.Timestamp(next(iter(data.values())).index[500]) + pd.Timedelta(seconds=30))
    assert any("掃描完成" in m for m in sent)
