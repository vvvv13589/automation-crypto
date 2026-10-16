"""AI trader (paper): fake exchange, fake AI, fake news (no network, no claude)."""

import copy
import json

import pytest

from cryptobot.aitrader import DEFAULTS, AITrader, Book, Guard, LiveBook, LiveError, News, parse_decision
from cryptobot.config import DEFAULTS as CFG

T0 = 1_789_999_980.0  # 2026-09-21 13:33 UTC, minute aligned


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class FakeEx:
    def __init__(self):
        self.px = {"BTC": 100_000.0, "ETH": 4000.0, "SOL": 200.0}
        self.bars: dict[str, list] = {}
        coins = list(self.px) + ["USDC"]
        self.markets = {f"{c}/USDT:USDT": {"swap": True, "quote": "USDT", "base": c, "active": True,
                                           "info": {"contractType": "PERPETUAL", "underlyingType": "COIN"}}
                        for c in coins}
        self.markets["TSLA/USDT:USDT"] = {"swap": True, "quote": "USDT", "base": "TSLA",
                                          "info": {"contractType": "PERPETUAL", "underlyingType": "EQUITY"}}

    def fetch_tickers(self):
        out = {s: {"last": self.px.get(m["base"], 1.0), "percentage": 1.0, "quoteVolume": 1e9 - i}
               for i, (s, m) in enumerate(self.markets.items())}
        return out

    def fetch_ticker(self, sym):
        return {"last": self.px[sym.split("/")[0]]}

    def fetch_funding_rates(self):
        return {f"{c}/USDT:USDT": {"fundingRate": 0.0001} for c in self.px}

    def fetch_ohlcv(self, sym, tf, since=None, limit=100):
        c = sym.split("/")[0]
        if tf == "1m":
            return [b for b in self.bars.get(c, []) if since is None or b[0] >= since]
        p = self.px[c]
        return [[T0 * 1000 - (60 - i) * 14_400_000, p, p * 1.01, p * 0.99, p, 1] for i in range(60)]


class FakeAI:
    def __init__(self):
        self.replies, self.prompts = [], []

    def decide(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0), {"total_cost_usd": 0.1, "num_turns": 3}


@pytest.fixture
def setup(tmp_path):
    cfg = copy.deepcopy(CFG)
    cfg.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    ex, ai, clock, sent = FakeEx(), FakeAI(), Clock(), []
    rss = b"<rss><channel><item><title>Bitcoin steady</title><pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>"
    fetch = lambda url: rss if "binance" not in url else json.dumps({"data": {"catalogs": []}}).encode()
    t = AITrader(cfg, ex, ai, notify=sent.append, clock=clock, fetch=fetch)
    return t, ex, ai, clock, sent


def test_parse_decision_takes_last_json_block():
    text = 'thinking {not json}\n```json\n{"view": "a", "actions": []}\n```\nmore\n```json\n{"view": "b"}\n```'
    assert parse_decision(text) == {"view": "b", "actions": []}
    with pytest.raises(ValueError):
        parse_decision("no json here")


def test_universe_skips_stables_and_stocks(setup):
    t, *_ = setup
    assert t.market.universe() == ["BTC", "ETH", "SOL"]


def test_open_sizes_by_risk_and_caps_leverage(setup):
    t, ex, ai, clock, sent = setup
    ai.replies.append({"view": "SOL 突破", "actions": [
        {"type": "open", "coin": "SOL", "side": "long", "stop": 190, "risk_pct": 9, "reason": "breakout"}]})
    t.decide("test")
    p = t.book.positions["SOL"]
    # risk clamped to 5% of 200 = 10 U over a 5% stop = 200 U notional (1x)
    assert p["qty"] * 200 == pytest.approx(200, rel=0.01)
    assert "開多 SOL" in sent[-1]
    # a 0.5% stop at 5% risk would be 10x: capped by total leverage 5x
    ai.replies.append({"view": "", "actions": [
        {"type": "open", "coin": "ETH", "side": "short", "stop": 4020, "risk_pct": 5, "reason": "x"}]})
    t.decide("test")
    assert t.book.notional({"SOL": 200, "ETH": 4000}) == pytest.approx(5 * t.book.equity({"SOL": 200, "ETH": 4000}),
                                                                        rel=0.01)


def test_guard_rejections(setup):
    t, ex, ai, clock, sent = setup
    ai.replies.append({"view": "", "actions": [
        {"type": "open", "coin": "SOL", "side": "long", "reason": "no stop"},
        {"type": "open", "coin": "SOL", "side": "long", "stop": 210, "reason": "wrong side"},
        {"type": "open", "coin": "DOGE", "side": "long", "stop": 0.1, "reason": "not in universe"},
        {"type": "open", "coin": "SOL", "side": "long", "stop": 190, "risk_pct": 1, "reason": "ok"},
        {"type": "open", "coin": "SOL", "side": "long", "stop": 190, "risk_pct": 1, "reason": "add"},
        {"type": "move_stop", "coin": "SOL", "stop": 180, "reason": "widen"},
    ]})
    t.decide("test")
    acts = json.loads(t.journal.read_text(encoding="utf-8").splitlines()[-1])["actions"]
    assert [a["ok"] for a in acts] == [False, False, False, True, False, False]
    assert "只能收緊" in acts[-1]["result"]
    assert "沒有執行" in sent[-1]


def test_floor_blocks_new_entries(setup):
    t, *_ = setup
    t.book.cash = 100
    ok, msg, _ = Guard(t.cfg).check_open({"coin": "SOL", "side": "long", "stop": 190}, t.book, 200, {}, ["SOL"])
    assert not ok and "底線" in msg


def test_stop_hit_on_later_minute_only(setup):
    t, ex, ai, clock, sent = setup
    ai.replies.append({"view": "", "actions": [
        {"type": "open", "coin": "SOL", "side": "long", "stop": 190, "take_profit": 230, "risk_pct": 2, "reason": "x"}]})
    clock.t += 30  # entry in the middle of a minute
    t.decide("test")
    m = int(T0 * 1000)
    # the entry minute traded below the stop before we entered: must not count
    ex.bars["SOL"] = [[m, 200, 201, 185, 200, 1]]
    clock.t += 60
    t.check_exits()
    assert "SOL" in t.book.positions
    ex.bars["SOL"].append([m + 60_000, 195, 196, 189, 190, 1])
    clock.t += 60
    t.check_exits()
    assert "SOL" not in t.book.positions
    row = open(t.book.trades_csv, encoding="utf-8").read()
    assert "停損" in row and "-1.0" in row  # about -1R after fees
    assert "🛑" in sent[-1]


def test_gap_through_stop_fills_at_open(tmp_path):
    cfg = {**DEFAULTS}
    b = Book(tmp_path / "b.json", tmp_path / "t.csv", cfg, 200)
    b.open("SOL", "short", 100, 1, 105, None, "x", T0)
    row = b.close("SOL", 110, "停損", T0 + 60)
    assert row["pnl"] < -10


def test_news_urgent_triggers_early_decision(setup):
    t, ex, ai, clock, sent = setup
    ai.replies += [{"view": "觀望", "actions": []}] * 3
    t.step()  # first news poll is the baseline, first decision runs
    assert len(ai.prompts) == 1 and "Bitcoin steady" in ai.prompts[0]
    t.news.fetch = lambda url: (b"<rss><channel><item><title>Exchange X hacked, $200M drained</title>"
                                b"<pubDate>Mon, 21 Sep 2026 10:30:00 +0000</pubDate></item></channel></rss>"
                                if "binance" not in url else b'{"data": {"catalogs": []}}')
    clock.t += 10 * 60
    t.step()
    assert len(ai.prompts) == 1  # urgent, but within 30 minutes of the last decision
    clock.t += 25 * 60
    t.step()
    assert len(ai.prompts) == 2 and "突發新聞" in ai.prompts[1]
    assert sent == []  # watching only: nothing pushed


def test_funding_charged_at_settlement(setup):
    t, ex, ai, clock, sent = setup
    t.book.open("SOL", "long", 200, 1, 190, None, "x", clock.t)
    t.funding()
    cash = t.book.cash
    clock.t += 8 * 3600
    t.funding()
    assert t.book.cash == pytest.approx(cash - 200 * 0.0001)


def test_state_survives_restart(setup):
    t, ex, ai, clock, sent = setup
    t.book.open("SOL", "long", 200, 1, 190, None, "x", clock.t)
    again = Book(t.book.path, t.book.trades_csv, t.cfg, 200)
    assert again.positions["SOL"]["stop"] == 190


def test_news_parse_and_dedupe(tmp_path):
    rss = (b"<rss><channel><item><title>A</title><pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate></item>"
           b"<item><title>B</title></item></channel></rss>")
    n = News(tmp_path / "n.json", fetch=lambda u: rss if "binance" not in u else b"{}", clock=lambda: T0)
    assert {i["title"] for i in n.poll()} == {"A", "B"}
    assert n.poll() == []


# -- live book (fake broker: no exchange) ---------------------------------------------
class FakeFill:
    def __init__(self, amount, price, fee=0.01):
        self.amount, self.price, self.fee = amount, price, fee


class FakeBroker:
    def __init__(self, acct, symbol):
        self.acct, self.symbol = acct, symbol

    def equity(self, price):
        return self.acct.equity

    def free_margin(self, price):
        return self.acct.equity

    def use_leverage(self, lev):
        self.acct.log.append(("lev", self.symbol, lev))

    def _amount(self, a):
        return round(a, 3)

    def market_open(self, side, amount, price):
        if amount * price < 5:
            return None
        self.acct.pos[self.symbol.split("/")[0]] = amount if side == "long" else -amount
        return FakeFill(round(amount, 3), price * 1.0002)

    def market_close(self, side, amount, price, since=None):
        coin = self.symbol.split("/")[0]
        px = price if self.acct.pos.pop(coin, None) is not None else self.acct.external_px
        self.acct.log.append(("close", coin, px))
        return FakeFill(amount, px)

    def set_stop(self, side, amount, stop, old_id=None):
        if self.acct.stop_fails:
            raise RuntimeError("stop rejected")
        self.acct.log.append(("stop", stop, old_id))
        if old_id:
            self.cancel_stop(old_id)
        return f"S{stop}"

    def cancel_stop(self, oid):
        if oid:
            self.acct.log.append(("cancel", oid))


class FakeAcct:
    def __init__(self):
        self.equity, self.pos, self.log, self.stop_fails, self.external_px = 50.0, {}, [], False, None

    def fetch_positions(self):
        return [{"symbol": f"{c}/USDT:USDT", "contracts": abs(a), "contractSize": 1,
                 "side": "long" if a > 0 else "short"} for c, a in self.pos.items()]

    def price_to_precision(self, sym, p):
        return str(p)

    def create_order(self, sym, kind, side, amount, price, params):
        self.log.append(("tp", params["takeProfitPrice"]))
        return {"id": f"T{params['takeProfitPrice']:g}"}

    def market(self, sym):
        return {"limits": {"cost": {"min": 5}}}


@pytest.fixture
def live(tmp_path):
    acct = FakeAcct()
    cfg = {**DEFAULTS}
    b = LiveBook(tmp_path / "l.json", tmp_path / "l.csv", cfg, 50, acct, clock=lambda: T0,
                 broker_factory=lambda s: FakeBroker(acct, s))
    return b, acct


def test_live_open_places_stop_and_tp(live):
    b, acct = live
    p = b.open("SOL", "long", 200, 0.1, 190, 220, "x", T0)
    assert p["stop_id"] == "S190" and p["tp_id"] == "T220"
    assert ("lev", "SOL/USDT:USDT", 5) in acct.log  # 5% stop -> 1/(0.07) = 14 -> capped at 5x
    b.open("ETH", "short", 4000, 0.003, 4800, None, "x", T0)  # 20% stop -> lower leverage
    assert ("lev", "ETH/USDT:USDT", 4) in acct.log


def test_live_stop_failure_closes_at_once(live):
    b, acct = live
    acct.stop_fails = True
    with pytest.raises(LiveError, match="停損掛單失敗"):
        b.open("SOL", "long", 200, 0.1, 190, None, "x", T0)
    assert acct.pos == {} and "SOL" not in b.positions


def test_live_too_small_is_rejected(live):
    b, acct = live
    with pytest.raises(LiveError, match="最低"):
        b.open("SOL", "long", 200, 0.01, 190, None, "x", T0)


def test_live_exchange_stop_detected_and_orders_cancelled(live):
    b, acct = live
    b.open("SOL", "long", 200, 0.1, 190, 220, "x", T0)
    del acct.pos["SOL"]  # the exchange stop fired
    acct.external_px = 189.9
    [(row, icon)] = b.sync_exits(None, T0 + 600)
    assert row["why"] == "停損" and icon == "🛑"
    assert ("cancel", "S190") in acct.log and ("cancel", "T220") in acct.log


def test_live_move_stop_replaces_order(live):
    b, acct = live
    b.open("SOL", "long", 200, 0.1, 190, None, "x", T0)
    b.move_stop("SOL", 195, T0 + 60)
    assert b.positions["SOL"]["stop_id"] == "S195" and ("cancel", "S190") in acct.log


def test_live_refuses_wrong_account(live):
    b, acct = live
    assert b.check_account() is None
    acct.pos["ETH"] = 1  # e.g. the main bot's position: not the AI sub-account
    assert "不是 AI 開的倉位" in b.check_account()
    acct.pos.clear()
    acct.equity = 400
    assert "超過" in b.check_account()
    acct.equity = 0  # USDT still in the spot wallet
    assert "U 本位合約" in b.check_account()


def test_live_zero_margin_is_a_clean_rejection(live):
    b, acct = live
    acct.equity = 0

    def precision_error(side, amount, price):
        raise RuntimeError("binance amount of SOL/USDT:USDT must be greater than minimum amount precision of 1")

    b.broker("SOL").market_open = precision_error
    with pytest.raises(LiveError, match="可用保證金 0.00"):
        b.open("SOL", "long", 200, 0.1, 190, None, "x", T0)


def test_decision_mirrors_to_paper_and_notifies_live_only(setup, live):
    t, ex, ai, clock, sent = setup
    lb, acct = live
    paper = t.book
    t.books, t.book = [lb, paper], lb
    ai.replies.append({"view": "做多 SOL", "actions": [
        {"type": "open", "coin": "SOL", "side": "long", "stop": 190, "risk_pct": 2, "reason": "x"}]})
    t.decide("test")
    assert "SOL" in lb.positions and "SOL" in paper.positions
    # same risk %, different account size: 2% of 50 vs 2% of 200 over a 5% stop
    assert lb.positions["SOL"]["qty"] * 200 == pytest.approx(20, rel=0.02)
    assert paper.positions["SOL"]["qty"] * 200 == pytest.approx(80, rel=0.02)
    assert len(sent) == 1 and "開多 SOL" in sent[0]
    row = json.loads(t.journal.read_text(encoding="utf-8").splitlines()[-1])
    assert row["mirror"]["模擬"][0]["ok"]
