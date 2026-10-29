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
    assert len(sent) == 2 and sent[0] == "👀 觀望\n\n觀望"  # every decision is pushed


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


# -- conditional entries + daily loss limit ---------------------------------------------
def plan(**kw):
    return {"type": "plan", "coin": "SOL", "side": "long", "trigger": 210, "confirm": "touch", "stop": 200,
            "risk_pct": 1, "reason": "breakout", **kw}


def test_plan_touch_opens_on_every_book(setup, tmp_path):
    t, ex, ai, clock, sent = setup
    mirror = Book(tmp_path / "m.json", tmp_path / "m.csv", t.cfg, 400, clock)
    t.books.append(mirror)
    ai.replies.append({"view": "SOL 等突破", "actions": [plan()]})
    t.decide("test")
    assert len(t.plans) == 1 and "掛條件單" in sent[-1] and "碰到站上 210" in sent[-1]
    m = int(T0 * 1000)
    ex.bars["SOL"] = [[m + 60_000, 200, 205, 199, 204, 1]]
    clock.t += 120
    t.check_plans()
    assert t.plans and "SOL" not in t.book.positions  # not reached yet
    ex.bars["SOL"].append([m + 180_000, 204, 211, 203, 210, 1])
    ex.px["SOL"] = 210.5
    clock.t += 120
    t.check_plans()
    assert t.plans == [] and "SOL" in t.book.positions and "SOL" in mirror.positions
    assert sent[-1].startswith("⚡ 條件單觸發") and "開多 SOL" in sent[-1]
    assert t.book.positions["SOL"]["stop"] == 200


def test_plan_needs_a_closed_candle_beyond_the_trigger(setup):
    t, ex, ai, clock, sent = setup
    assert t.add_plan(plan(confirm="1h"), ["SOL"])["ok"]
    h = 3_600_000
    start = (int(clock.t * 1000) // h + 1) * h  # the first 1h candle that opens after the plan
    real = ex.fetch_ohlcv
    ex.fetch_ohlcv = lambda sym, tf, since=None, limit=100: (
        [[start, 205, 212, 204, 209, 1], [start + h, 209, 215, 208, 214, 1]] if tf == "1h" else real(sym, tf, since, limit))
    clock.t = start / 1000 + 1800  # first candle still open (it wicked above 210)
    t.check_plans()
    assert t.plans and "SOL" not in t.book.positions
    clock.t = start / 1000 + h / 1000 + 60  # it closed at 209: still below the trigger
    t.check_plans()
    assert t.plans
    clock.t = start / 1000 + 2 * h / 1000 + 60  # the next one closed at 214
    ex.px["SOL"] = 214
    t.check_plans()
    assert "SOL" in t.book.positions


def test_plan_validation_and_expiry(setup):
    t, ex, ai, clock, sent = setup
    assert "已經在觸發價上方" in t.add_plan(plan(trigger=190, when="above"), ["SOL"])["result"]
    assert "停損離觸發價" in t.add_plan(plan(stop=209.9), ["SOL"])["result"]
    assert "不在前" in t.add_plan(plan(coin="DOGE"), ["SOL"])["result"]
    assert t.add_plan(plan(expires_hours=2), ["SOL"])["ok"]
    assert t.add_plan(plan(trigger=212), ["SOL"])["result"] == "更新條件單" and len(t.plans) == 1
    assert t.cancel_plan({"coin": "SOL"})["ok"] and t.plans == []
    t.add_plan(plan(expires_hours=2), ["SOL"])
    clock.t += 3 * 3600
    t.check_plans()
    assert t.plans == [] and "SOL" not in t.book.positions


def test_daily_loss_limit(setup):
    t, ex, ai, clock, sent = setup
    t.book.mark_day(clock.t, lambda: 200.0)
    t.book.cash = 179  # -10.5% today
    ok, msg, _ = Guard(t.cfg).check_open({"coin": "SOL", "side": "long", "stop": 190}, t.book, 200, {}, ["SOL"])
    assert not ok and "今天已虧" in msg
    clock.t += 86400  # next Taipei day: new reference
    t.book.mark_day(clock.t, lambda: 179.0)
    ok, msg, _ = Guard(t.cfg).check_open({"coin": "SOL", "side": "long", "stop": 190}, t.book, 200, {}, ["SOL"])
    assert ok


def test_status_text_for_telegram(setup, live):
    from cryptobot.aitrader import status_text
    t, ex, ai, clock, sent = setup
    lb, acct = live
    raw = {"state_dir": str(t.plans_path.parent), "log_dir": str(t.journal.parent)}
    lb.path = t.plans_path.parent / "aitrader_live_book.json"
    lb.open("SOL", "short", 200, 0.1, 210, 180, "x", T0)
    t.add_plan(plan(), ["SOL"])
    ai.replies.append({"view": "BTC 盤整\n\nSOL 空單續抱", "actions": []})
    t.decide("test")
    text = status_text(raw, lambda c: 190.0, live_equity=51.0, now=T0)
    assert "權益 51.00 U（起始 50 U，+2.0%）" in text
    assert "SOL 空 0.1 @ 200" in text and "+1.00 U" in text and "停損 210" in text
    assert "條件單 1 張" in text and "SOL 空單續抱" in text


# -- full-market scan ---------------------------------------------------------------
def bars(closes, vol=1.0):
    return [[i, c, c * 1.01, c * 0.99, c, vol] for i, c in enumerate(closes)]


def test_scan_flags_breakout_with_oi_and_volume():
    from cryptobot.aitrader import scan_coin
    d1 = bars([100 + i * 0.5 for i in range(59)] + [140])  # steady uptrend, today above the 20-day high
    h4 = bars([100 + i * 0.6 for i in range(60)])
    h1 = bars([130] * 24 + [130 + i for i in range(24)], vol=20)  # heavy last-24h volume
    oi = [100.0] * 24 + [130.0]
    ls = {"top": [1.0] * 24 + [0.8], "retail": [1.5] * 25, "taker": [(150, 100)] * 4}
    s = scan_coin({"h1": h1, "h4": h4, "d1": d1, "oi": oi, "ls": ls}, 0.0008, btc7=1.0)
    assert s["d_struct"] == "升" and s["h4_struct"] == "升"
    for flag in ("站上 20 日高點", "持倉量 24h +30%", "價漲量增", "資金費率偏多", "日線與 4h 同步上升",
                 "大戶偏空、散戶偏多", "主動買盤強"):
        assert any(f.startswith(flag) for f in s["flags"]), (flag, s["flags"])
    assert any(f.startswith("成交量") for f in s["flags"])


def test_scan_quiet_coin_has_no_flags():
    from cryptobot.aitrader import scan_coin
    flat = bars([100 + (i % 2) * 0.1 for i in range(60)])
    s = scan_coin({"h1": flat[-48:], "h4": flat, "d1": [[i, 100, 105, 95, 100, 24] for i in range(60)], "oi": [100.0] * 25},
                  0.0001, btc7=0.0)
    assert s["flags"] == [] and s["d_struct"] == "盤"


def test_digest_lists_every_coin_and_the_flagged_ones(setup):
    t, *_ = setup
    text = t.market.digest(["BTC", "ETH", "SOL"])
    rows = [l for l in text.splitlines() if l.split(" | ")[0] in ("BTC", "ETH", "SOL")]
    assert len(rows) == 3 and "程式篩選出的訊號" in text


def _trade_rows(book, rows):
    import csv
    with book.trades_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["coin", "side", "opened", "closed", "entry", "exit", "qty", "pnl", "r", "why", "reason"])
        w.writeheader()
        w.writerows(rows)


def test_weekly_review_rewrites_lessons_shown_in_later_prompts(setup):
    from datetime import datetime, timezone
    t, ex, ai, clock, sent = setup
    clock.t = datetime(2026, 9, 27, 2, 5, tzinfo=timezone.utc).timestamp()  # Sunday 10:05 Taipei
    _trade_rows(t.book, [
        {"coin": "SOL", "side": "long", "opened": "2026-09-25 10:00", "closed": "2026-09-25 14:00", "entry": "200",
         "exit": "196", "qty": "1", "pnl": "-4", "r": "-1", "why": "停損", "reason": "突破追單"},
        {"coin": "ETH", "side": "short", "opened": "2026-09-10 10:00", "closed": "2026-09-11 10:00", "entry": "4000",
         "exit": "3900", "qty": "0.1", "pnl": "10", "r": "2", "why": "止盈", "reason": "old"}])
    prompts = []
    ai.review = lambda p: (prompts.append(p) or ({"review": "這週突破追單都輸。", "lessons": ["突破等 1h 收盤確認再進（1 筆，待觀察）"]},
                                                 {"total_cost_usd": 0.2}))
    t.maybe_review()
    assert "SOL long" in prompts[0] and "old" not in prompts[0]  # only the last 7 days
    assert "📝 AI 每週檢討（1 筆）" in sent[-1] and "突破等 1h 收盤確認" in sent[-1]
    assert t.lessons()["lessons"] == ["突破等 1h 收盤確認再進（1 筆，待觀察）"]
    assert "突破等 1h 收盤確認" in t.build_prompt([], {}, "test")
    n = len(sent)
    t.maybe_review()  # once a day
    assert len(sent) == n
    clock.t += 6 * 86400  # Saturday: not the review day
    t.maybe_review()
    assert len(sent) == n


def test_position_milestones_wake_the_ai_once(setup):
    t, ex, ai, clock, sent = setup
    t.book.open("SOL", "long", 200, 1, 190, None, "x", clock.t)  # 1R = 10
    t.last_decision = t.last_news = clock.t  # hourly decision not due, news already polled
    clock.t += 20 * 60
    ex.px["SOL"] = 211
    ai.replies.append({"view": "考慮移停損", "actions": []})
    t.step()
    assert "持倉事件：SOL 浮盈首次到 +1R" in ai.prompts[-1]
    assert t.book.positions["SOL"]["hit_1R"] and not t.book.positions["SOL"].get("hit_2R")
    n = len(ai.prompts)
    ex.px["SOL"] = 221
    t.last_news = clock.t = clock.t + 5 * 60
    t.step()  # +2R, but the last decision was only 5 minutes ago: waits
    assert len(ai.prompts) == n
    t.last_news = clock.t = clock.t + 11 * 60
    ai.replies.append({"view": "", "actions": []})
    t.step()
    assert "SOL 浮盈首次到 +2R" in ai.prompts[-1] and "+1R" not in ai.prompts[-1].split("\n")[0]
    n = len(ai.prompts)
    t.last_news = clock.t = clock.t + 20 * 60
    t.step()  # both levels already seen: nothing new
    assert len(ai.prompts) == n


def test_fast_move_against_a_position_wakes_the_ai(setup):
    t, ex, ai, clock, sent = setup
    t.cfg["event_move_r"] = 0.5
    t.book.open("SOL", "long", 200, 1, 190, None, "x", clock.t)
    t.last_decision, t.last_news = clock.t - 20 * 60, clock.t
    t.step()  # first look at the price: nothing to compare with yet
    assert not ai.prompts
    clock.t += 5 * 60
    t.last_news = clock.t
    ex.px["SOL"] = 194
    ai.replies.append({"view": "", "actions": []})
    t.step()
    assert "SOL 15 分鐘內下跌 0.6R" in ai.prompts[-1] and "多單" in ai.prompts[-1]


def test_excursions_during_and_after_a_trade(setup):
    from datetime import datetime, timezone
    t, ex, *_ = setup
    ms = lambda h, m=0: int(datetime(2026, 9, 27, h - 8, m, tzinfo=timezone.utc).timestamp() * 1000)  # Taipei h:m
    ex.fetch_ohlcv = lambda sym, tf, since=None, limit=100: [
        [ms(10, 15), 100, 106, 99, 104, 1],   # while open: best +6, worst -1
        [ms(13), 104, 110, 103, 108, 1]]      # after the close
    trade = {"coin": "SOL", "side": "long", "opened": "2026-09-27 10:00", "closed": "2026-09-27 12:00",
             "entry": "100", "exit": "104", "qty": "1", "pnl": "4", "r": "2"}  # 1R = 2
    assert t.excursions(trade) == {"best_R": 3.0, "worst_R": -0.5, "after_24h_further_R": 3.0, "after_24h_against_R": 0.5}


def add_tradfi(ex):
    for base, kind, px in (("XAU", "COMMODITY", 4000.0), ("NVDA", "EQUITY", 180.0), ("OPENAI", "PREMARKET", 50.0),
                           ("USDBRL", "FX", 5.0), ("TINY", "EQUITY", 10.0)):
        ex.px[base] = px
        ex.markets[f"{base}/USDT:USDT"] = {"swap": True, "quote": "USDT", "base": base, "active": True,
                                           "info": {"contractType": "TRADIFI_PERPETUAL", "underlyingType": kind}}
    plain = ex.fetch_tickers
    ex.fetch_tickers = lambda: {s: {**t, "quoteVolume": 1e6} if s.startswith("TINY") else t for s, t in plain().items()}


def test_universe_adds_tradfi_after_crypto(setup):
    t, ex, *_ = setup
    add_tradfi(ex)
    # crypto first, then TradFi above the volume floor; no pre-IPO, FX, or old-style stock tokens
    assert t.market.universe() == ["BTC", "ETH", "SOL", "XAU", "NVDA"]
    assert t.market.kinds["NVDA"] == "EQUITY" and t.market.kinds["BTC"] == "COIN"
    t.market.tradfi = 0
    assert t.market.universe() == ["BTC", "ETH", "SOL"]


def test_market_sessions():
    from datetime import datetime, timezone
    from cryptobot.aitrader import session
    at = lambda *a: datetime(*a, tzinfo=timezone.utc).timestamp()
    assert session("COIN", at(2026, 10, 24, 3, 0))[0]
    # US stocks, Tuesday 2026-10-20 (EDT): 10:00 New York is open with 6 h left, 17:00 is closed
    assert session("EQUITY", at(2026, 10, 20, 14, 0)) == (True, 360.0, "美股·開盤")
    assert not session("EQUITY", at(2026, 10, 20, 21, 0))[0]
    assert not session("EQUITY", at(2026, 10, 24, 15, 0))[0]  # Saturday
    assert session("KR_EQUITY", at(2026, 10, 20, 1, 0))[:2] == (True, 330.0)  # 10:00 Seoul
    assert not session("HK_EQUITY", at(2026, 10, 20, 4, 30))[0]  # 12:30 Hong Kong lunch break
    # futures: daily break 17:00–18:00 New York, closed from Friday 17:00 to Sunday 18:00
    assert not session("COMMODITY", at(2026, 10, 20, 21, 30))[0]
    assert session("COMMODITY", at(2026, 10, 20, 22, 30))[0]
    assert not session("COMMODITY", at(2026, 10, 24, 12, 0))[0]
    assert session("COMMODITY", at(2026, 10, 25, 22, 30))[0]  # Sunday 18:30 New York


def test_guard_leaves_market_hours_to_the_ai(setup):
    t, *_ = setup
    a = {"coin": "SOL", "side": "long", "stop": 190}
    ok, _, _ = Guard(t.cfg).check_open(a, t.book, 200, {}, ["SOL"])
    assert ok
    t.market.session = lambda c: (True, 45.0, "美股·開盤")
    assert t.market.session_tag("NVDA") == "美股·開盤，45 分鐘後收盤"
    t.market.session = lambda c: (True, 300.0, "美股·開盤")
    assert t.market.session_tag("NVDA") == "美股·開盤"
    t.market.session = lambda c: (False, 0.0, "美股·休市")
    assert t.market.session_tag("NVDA") == "美股·休市"


def test_digest_has_a_tradfi_section_with_the_session(setup):
    t, ex, *_ = setup
    add_tradfi(ex)
    coins = t.market.universe()
    text = t.market.digest(coins)
    assert "傳統市場" in text and "XAU〔期貨·" in text and "NVDA〔美股·" in text


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
