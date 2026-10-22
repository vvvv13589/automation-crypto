"""Taiwan stock advisor: fake data, fake AI (no network, no claude)."""

import json
from datetime import date, datetime, timedelta

import pytest

from cryptobot.twstock import (FUT, TZ, Advisor, Book, Data, scan_stock, settings, streak)


def ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_bars(n=260, start=100.0, step=0.2, end=date(2026, 10, 21)):
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    out = []
    for i, d in enumerate(reversed(days)):
        c = start + step * i
        out.append({"t": datetime(d.year, d.month, d.day, 9, tzinfo=TZ).timestamp(), "date": d.strftime("%Y%m%d"),
                    "o": c - 0.1, "h": c + 0.5, "l": c - 0.5, "c": c, "v": 1_000_000})
    return out


class FakeData:
    def __init__(self):
        self.q: dict[str, dict] = {}
        self.tx = None
        self.bars = {"2330": make_bars(start=60), "2317": make_bars(start=150, step=-0.1), "^TWII": make_bars(start=40000, step=20)}
        for sym, _ in __import__("cryptobot.twstock", fromlist=["GLOBAL"]).GLOBAL:
            self.bars.setdefault(sym, make_bars(n=63, start=100))

    def trading_day(self, d):
        return d.weekday() < 5 and d != date(2026, 10, 26)

    def add_trading_days(self, d, n):
        while n > 0:
            d += timedelta(days=1)
            n -= self.trading_day(d)
        return d

    last_days = Data.last_days

    def index_bars(self, d):
        return []

    def universe(self, n):
        return [{"code": "2330", "name": "台積電", "industry": "半導體", "cap": 2},
                {"code": "2317", "name": "鴻海", "industry": "其他電子", "cap": 1}][:n]

    def many_bars(self, symbols, rng="1y"):
        return {s: self.bars[s] for s in symbols if s in self.bars}

    def chips(self, d):
        return {"2330": (2_000_000, 500_000, -10_000), "2317": (-1_000_000, 0, 0)}

    def margin(self, d):
        return {"2330": (10_000, 100), "2317": (50_000, 5_000)}, 300_000_000

    def revenue(self):
        return {"2330": {"ym": "11509", "mom": 5.0, "yoy": 40.0, "cum": 35.0}}

    def alerts(self):
        return {}

    def futures(self):
        return {"date": "20261021", "month": "202611",
                "day": {"Last": "47000", "Change": "+100", "High": "47100", "Low": "46800"},
                "night": {"Last": "47200", "Change": "+200", "High": "47300", "Low": "46900"},
                "oi": {"20261020": {"外資及陸資": -80000, "投信": 80000, "自營商": 1000},
                       "20261021": {"外資及陸資": -86000, "投信": 83000, "自營商": 1700}}}

    def turnover(self, d):
        return {"20261020": 8e11, "20261021": 9e11}

    def institutions(self, d):
        return {"外資及陸資(不含外資自營商)": 5e9, "投信": 1e9, "自營商(自行買賣)": -2e8, "自營商(避險)": 1e8, "合計": 5.9e9}

    def news(self):
        return [{"src": "鉅亨台股", "title": "台積電法說會", "ts": ts(2026, 10, 22, 8)}]

    def quotes(self, codes):
        return {c: self.q[c] for c in codes if c in self.q}

    def tx_quote(self):
        return self.tx


class FakeAI:
    def __init__(self, decision=None):
        self.decision = decision or {"view": "觀望", "actions": []}
        self.prompts = []

    def decide(self, prompt):
        self.prompts.append(prompt)
        return json.loads(json.dumps(self.decision)), {"total_cost_usd": 0.1}


@pytest.fixture
def env(tmp_path):
    clock = Clock(ts(2026, 10, 21, 16, 30))
    data, msgs = FakeData(), []
    cfg = {"state_dir": str(tmp_path / "state"), "log_dir": str(tmp_path / "logs")}
    adv = Advisor(cfg, data, FakeAI(), msgs.append, clock, work_dir=tmp_path / "work")
    adv.names = {"2330": "台積電", "2317": "鴻海"}
    adv.closes = {"2330": 100.0, "2317": 150.0, FUT: 47000.0}
    return adv, data, clock, msgs


def q(o, h, l, z, d="20261022", u=None):
    return {"date": d, "o": o, "h": h, "l": l, "z": z, "y": None, "u": u, "w": None}


def test_streak():
    assert streak([1, -2, 3, 4, 5]) == 3
    assert streak([1, -2, -3]) == -2
    assert streak([1, 0]) == 0


def test_scan_flags():
    bars = make_bars()
    chips = [(1e6, 2e5, 0)] * 10
    s = scan_stock(bars, 1.0, chips, [(1000, 10)] * 6, {"yoy": 40, "cum": 30}, None)
    assert s["trend"] == "多頭排列" and s["struct"] == "升"
    assert s["fs"] == 10 and s["ts"] == 10
    assert "投信連買10天" in s["flags"] and "外資連買10天" in s["flags"] and "營收年增>30%" in s["flags"]
    assert s["f5"] == pytest.approx(5000)


def test_open_fills_at_next_open(env):
    adv, data, clock, msgs = env
    r = adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 95, "take_profit": 115, "risk_pct": 1,
                   "reason": "回測月線"})
    assert r["ok"] and adv.book.pending and "約 2,000" not in r["msg"]
    clock.t = ts(2026, 10, 22, 9, 5)
    adv.fill_pending({"2330": q(101, 101.5, 100.5, 101)}, "20261022")
    p = adv.book.positions["2330"]
    assert p["entry"] == 101 and p["qty"] == int(10_000 / 6)
    assert not adv.book.pending and "✅" in msgs[-1]


def test_gap_and_limit_up_not_chased(env):
    adv, data, clock, msgs = env
    adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 95, "risk_pct": 1})
    adv.apply({"type": "open", "code": "2317", "side": "long", "stop": 140, "risk_pct": 1})
    adv.fill_pending({"2330": q(104, 104, 104, 104), "2317": q(152, 152, 152, 152, u=152)}, "20261022")
    assert not adv.book.positions and not adv.book.pending
    assert any("跳空" in m for m in msgs) and any("漲停" in m for m in msgs)


def test_pending_waits_for_open(env):
    adv, *_ = env
    adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 95, "risk_pct": 1})
    adv.fill_pending({"2330": q(None, None, None, None)}, "20261022")
    adv.fill_pending({"2330": q(100, 100, 100, 100, d="20261021")}, "20261022")
    assert len(adv.book.pending) == 1


def test_stop_and_gap_through_stop(env):
    adv, data, clock, msgs = env
    b = adv.book
    b.open({"code": "2330", "side": "long", "stop": 95, "risk_pct": 1, "tp": 120}, 100, ts(2026, 10, 21, 9), 1e6)
    b.open({"code": "2317", "side": "long", "stop": 140, "risk_pct": 1}, 150, ts(2026, 10, 21, 9), 1e6)
    clock.t = ts(2026, 10, 22, 9, 5)
    adv.check_exits({"2330": q(99, 99, 94, 94.5), "2317": q(130, 131, 129, 130)}, "20261022")
    tr = {t["code"]: t for t in b.trades()}
    assert float(tr["2330"]["exit"]) == 95 and float(tr["2330"]["r"]) < -1  # costs make it a bit worse than -1R
    assert float(tr["2317"]["exit"]) == 130  # gapped below the stop: filled at the open
    assert not b.positions


def test_rejections(env):
    adv, *_ = env
    assert "只做多" in adv.apply({"type": "open", "code": "2330", "side": "short", "stop": 105})["result"]
    assert "停損距離" in adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 99.5})["result"]
    assert "不在市值前" in adv.apply({"type": "open", "code": "9999", "side": "long", "stop": 90})["result"]
    assert "沒有停損" in adv.apply({"type": "open", "code": "2330", "side": "long"})["result"]
    assert adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 95, "risk_pct": 2})["ok"]
    assert "不加碼" in adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 95})["result"]


def test_total_risk_cap(env):
    adv, *_ = env
    adv.cfg["max_open_risk_pct"] = 3
    assert adv.apply({"type": "open", "code": "2330", "side": "long", "stop": 95, "risk_pct": 2})["ok"]
    assert "一起打到停損" in adv.apply({"type": "open", "code": "2317", "side": "long", "stop": 140, "risk_pct": 2})["result"]


def test_future_short_sizing(env):
    adv, data, clock, msgs = env
    r = adv.apply({"type": "open", "code": "TX", "side": "short", "stop": 47300, "take_profit": 46000, "risk_pct": 1})
    assert r["ok"] and "3 口微台" in r["msg"]
    data.tx = q(46950, 47000, 46800, 46900)
    adv.fill_pending({FUT: data.tx}, "20261022")
    p = adv.book.positions[FUT]
    assert p["qty"] == 2 and p["entry"] == 46950  # 10,000 / (350 points × 10)
    clock.t = ts(2026, 10, 23, 9, 5)
    adv.check_exits({FUT: q(46950, 47350, 46800, 47300, d="20261023")}, "20261023")
    t = adv.book.trades()[-1]
    assert float(t["exit"]) == 47300 and int(t["pnl"]) == -(350 + 2) * 2 * 10


def test_move_stop_only_tightens(env):
    adv, *_ = env
    adv.book.open({"code": "2330", "side": "long", "stop": 95, "risk_pct": 1}, 100, ts(2026, 10, 20, 9), 1e6)
    assert not adv.apply({"type": "move_stop", "code": "2330", "stop": 94})["ok"]
    assert not adv.apply({"type": "move_stop", "code": "2330", "stop": 101})["ok"]  # above the last close
    assert adv.apply({"type": "move_stop", "code": "2330", "stop": 98})["ok"]
    assert adv.book.positions["2330"]["stop"] == 98 and adv.book.positions["2330"]["init_stop"] == 95


def test_close_at_next_open(env):
    adv, *_ = env
    adv.book.open({"code": "2330", "side": "long", "stop": 95, "risk_pct": 1}, 100, ts(2026, 10, 20, 9), 1e6)
    assert adv.apply({"type": "close", "code": "2330", "reason": "投信轉賣"})["ok"]
    adv.fill_pending({"2330": q(103, 103, 103, 103)}, "20261022")
    t = adv.book.trades()[-1]
    assert float(t["exit"]) == 103 and float(t["pnl"]) > 0


def test_close_confirm_plan(env):
    adv, data, clock, msgs = env
    r = adv.apply({"type": "plan", "code": "2330", "side": "long", "trigger": 105, "when": "above", "confirm": "close",
                   "stop": 99, "risk_pct": 1, "expires_days": 3})
    assert r["ok"], r
    assert adv.book.plans[0]["expires"] == "20261027"  # 10/26 holiday skipped
    data.q["2330"] = q(103, 106, 102, 104.5)
    adv.after_close("20261022")
    assert adv.book.plans and not adv.book.pending  # closed below the trigger
    data.q["2330"] = q(104, 107, 104, 106, d="20261023")
    adv.after_close("20261023")
    assert not adv.book.plans and adv.book.pending[0]["ref"] == 106
    adv.fill_pending({"2330": q(106.5, 107, 106, 106.5, d="20261027")}, "20261027")
    assert adv.book.positions["2330"]["entry"] == 106.5


def test_plan_expires(env):
    adv, data, clock, msgs = env
    adv.apply({"type": "plan", "code": "2330", "side": "long", "trigger": 105, "confirm": "close", "stop": 99,
               "expires_days": 1})
    data.q["2330"] = q(100, 101, 99.5, 100)
    adv.after_close("20261022")
    assert not adv.book.plans and "到期" in msgs[-1]


def test_touch_plan_and_intraday_stop(env):
    adv, data, clock, msgs = env
    assert adv.apply({"type": "plan", "code": "2330", "side": "long", "trigger": 98, "when": "below",
                      "confirm": "touch", "stop": 95, "risk_pct": 1})["ok"]
    clock.t = ts(2026, 10, 22, 10, 0)
    # earlier today it dipped to 94 before we filled: that low must not stop the new position
    adv.check_touch_plans({"2330": q(100, 100, 94, 97.5)}, "20261022")
    p = adv.book.positions["2330"]
    assert p["entry"] == 98 and p["intraday"]
    adv.check_exits({"2330": q(100, 100, 94, 97.5)}, "20261022")
    assert "2330" in adv.book.positions
    adv.check_exits({"2330": q(100, 100, 94, 94.8)}, "20261022")
    assert "2330" not in adv.book.positions


def test_plan_trigger_on_wrong_side(env):
    adv, *_ = env
    r = adv.apply({"type": "plan", "code": "2330", "side": "long", "trigger": 98, "when": "above", "stop": 92})
    assert not r["ok"] and "直接用 open" in r["result"]


def test_decide_end_to_end(env, tmp_path):
    adv, data, clock, msgs = env
    adv.ai = FakeAI({"view": "大盤偏多\n\n2330 台積電：投信連買", "watch": "2317：站上 140 再說",
                     "actions": [{"type": "open", "code": "2330", "side": "long", "stop": 100, "take_profit": 125,
                                  "risk_pct": 1, "reason": "法人連買"},
                                 {"type": "open", "code": "2317", "side": "short", "stop": 130}]})
    adv.decide("盤後")
    prompt = adv.ai.prompts[0]
    for part in ("加權指數", "三大法人現貨買賣超", "台指期 11 月", "外資台指期" if False else "三大法人台指期淨未平倉",
                 "台積電ADR", "2330 台積電|半導體", "台積電法說會", "投信連買"):
        assert part in prompt, part
    assert "🌆 盤後" in msgs[-1] and "🛒" in msgs[-1] and "⛔" in msgs[-1] and "等待" in msgs[-1]
    detail = json.loads((tmp_path / "work" / "chips.json").read_text(encoding="utf-8"))
    assert detail["2330"]["days"][-1]["外資張"] == 2000
    row = json.loads(adv.journal.read_text(encoding="utf-8").splitlines()[-1])
    assert row["trigger"] == "盤後" and row["actions"][0]["ok"]


def test_schedule(env):
    adv, data, clock, msgs = env
    calls = []
    adv.decide = calls.append
    adv.after_close = lambda today: calls.append("close " + today)
    clock.t = ts(2026, 10, 22, 8, 31)
    adv.step()
    adv.step()
    clock.t = ts(2026, 10, 22, 13, 55)
    adv.step()
    clock.t = ts(2026, 10, 22, 16, 31)
    adv.step()
    adv.step()
    assert calls == ["盤前", "close 20261022", "盤後"]
    clock.t = ts(2026, 10, 26, 8, 31)  # holiday
    adv.step()
    clock.t = ts(2026, 10, 24, 8, 31)  # Saturday
    adv.step()
    assert len(calls) == 3


def test_holidays_parse(tmp_path):
    rows = [{"Name": "國慶日", "Date": "1151009"}, {"Name": "農曆春節後開始交易日", "Date": "1150223"},
            {"Name": "市場無交易，僅辦理結算交割作業", "Date": "1150212"}]
    d = Data(tmp_path, fetch=lambda url, **kw: json.dumps(rows).encode())
    assert d.holidays() == {"20261009", "20260212"}
    assert not d.trading_day(date(2026, 10, 9)) and d.trading_day(date(2026, 2, 23))


def test_last_days_skip_holidays_and_open_session():
    d = FakeData()
    # 10/26 holiday, 10/27 still trading at 10:00
    assert Data.last_days(d, datetime(2026, 10, 27, 10, 0, tzinfo=TZ), 3) == ["20261021", "20261022", "20261023"]
    assert Data.last_days(d, datetime(2026, 10, 27, 16, 30, tzinfo=TZ), 2) == ["20261023", "20261027"]


def test_telegram_target_prefers_own_bot(monkeypatch):
    from cryptobot.twstock import telegram_target
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "main")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("TWSTOCK_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TWSTOCK_TELEGRAM_CHAT_ID", raising=False)
    assert telegram_target() == ("main", "1")
    monkeypatch.setenv("TWSTOCK_TELEGRAM_BOT_TOKEN", "tw")
    monkeypatch.setenv("TWSTOCK_TELEGRAM_CHAT_ID", "-100")
    assert telegram_target() == ("tw", "-100")


def test_now_during_session_fills_at_live_price(env):
    adv, data, clock, msgs = env
    clock.t = ts(2026, 10, 22, 10, 30)
    live = data.bars["2330"][-1]["c"]  # 111.8 last close; the live price differs
    data.q = {"2330": {**q(112, 116, 111.5, 115), "v": 30_000.0}, "2317": {**q(124, 125, 123, 124), "v": 1.0},
              "t00": q(40000, 40500, 39900, 40400)}
    data.q["t00"]["y"] = 40000.0
    data.q["2330"]["y"] = live
    adv.ai = FakeAI({"view": "盤中轉強", "actions": [{"type": "open", "code": "2330", "side": "long", "stop": 109,
                                                     "take_profit": 130, "risk_pct": 1, "reason": "帶量突破"}]})
    adv.handle("/now@twbot 2330")
    prompt = adv.ai.prompts[0]
    assert "盤中即時" in prompt and "使用者特別想問：2330 台積電" in prompt and "2330 台積電|115|" in prompt
    p = adv.book.positions["2330"]
    assert p["entry"] == 115 and p["intraday"] and not adv.book.pending
    assert "⚡ 即時（盤中）" in msgs[-1] and "現價 115 買進" in msgs[-1]
    assert not adv.live_now
    # a second /now right away is refused
    adv.handle("/now")
    assert "分鐘後再叫" in msgs[-1] and len(adv.ai.prompts) == 1


def test_now_after_close_waits_for_next_open(env):
    adv, data, clock, msgs = env
    clock.t = ts(2026, 10, 22, 20, 0)
    adv.ai = FakeAI({"view": "x", "actions": [{"type": "open", "code": "2330", "side": "long", "stop": 105,
                                               "risk_pct": 1}]})
    adv.handle("/now")
    assert "盤中即時" not in adv.ai.prompts[0] and "下一個開盤" in adv.ai.prompts[0]
    assert adv.book.pending and not adv.book.positions


def test_status_and_help(env):
    adv, data, clock, msgs = env
    adv.handle("/tw")
    assert "建議追蹤" in msgs[-1]
    adv.handle("/help")
    assert "/now" in msgs[-1] and "/ask" in msgs[-1]
    adv.handle("/whatever")
    assert len(msgs) == 2


class FakeChat:
    def __init__(self, answer="台積電 2,380 有支撐"):
        self.answer, self.prompts, self.session_id = answer, [], "s1"

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answer


def test_questions(env):
    adv, data, clock, msgs = env
    adv.chat = FakeChat()
    adv.ai = FakeAI({"view": "大盤偏弱", "watch": "2330：2,380 回測再買", "actions": []})
    adv.decide("盤後")
    adv.handle("台積電現在可以買嗎？")
    p = adv.chat.prompts[-1]
    assert "台積電現在可以買嗎？" in p and "不要輸出 JSON" in p and "大盤偏弱" in p and "2,380 回測再買" in p
    assert "last_scan.txt" in p and (adv.work_dir / "last_scan.txt").exists()
    assert msgs[-2] == "💬 想一下…" and msgs[-1] == "台積電 2,380 有支撐"
    adv.handle("/ask@twbot 聯發科呢")
    assert "聯發科呢" in adv.chat.prompts[-1]
    adv.handle("/ask")
    assert "問題寫在" in msgs[-1]
    adv.handle("/new")
    assert adv.chat.session_id is None and not adv.book.positions


def test_long_answer_is_split(env):
    adv, data, clock, msgs = env
    adv.chat = FakeChat("\n".join(f"第 {i} 行" + "字" * 50 for i in range(200)))
    adv.handle("講長一點")
    parts = msgs[1:]
    assert len(parts) >= 3 and all(len(m) <= 3800 for m in parts)
    assert "".join(parts).count("第 199 行") == 1


def test_chat_resumes_and_restarts_after_idle(tmp_path):
    from cryptobot.twstock import TWChat
    calls = []
    clock = Clock(1000.0)

    def run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode, stderr = 0, ""
            stdout = json.dumps({"result": "ok", "session_id": f"s{len(calls)}"})
        return R()
    chat = TWChat({}, str(tmp_path), "claude", "tok", "opus", 10, user=None, run=run, clock=clock)
    chat.ask("a")
    chat.ask("b")
    assert "--resume" not in calls[0] and calls[1][-2:] == ["--resume", "s1"]
    clock.t += 3 * 3600
    assert chat.ask("c").startswith("🆕") and "--resume" not in calls[2]


def test_commands_only_from_own_chat_and_skip_backlog():
    from cryptobot.twstock import Commands
    calls = []

    def fetch(url, timeout=None):
        calls.append(url)
        if "offset=-1" in url:
            return json.dumps({"result": [{"update_id": 7, "message": {"chat": {"id": -100}, "text": "/now"}}]}).encode()
        return json.dumps({"result": [
            {"update_id": 8, "message": {"chat": {"id": -100}, "text": "/now 2330"}},
            {"update_id": 9, "message": {"chat": {"id": 555}, "text": "/now"}}]}).encode()
    c = Commands("tok", "-100", fetch)
    assert c.poll(1) == ["/now 2330"]  # the queued /now from before the start is skipped
    assert "offset=8" in calls[1] and c.offset == 10


def test_settings_override():
    assert settings({"twstock": {"capital": 300000}})["capital"] == 300000
