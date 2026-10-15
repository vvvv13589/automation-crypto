"""Telegram butler and AI chat: fake exchange, fake Telegram, fake claude (no real accounts)."""

import copy
import json
import subprocess
from pathlib import Path

import pytest

from cryptobot.ai import ClaudeChat
from cryptobot.butler import HELP, Butler, account_snapshot, format_status, split_message
from cryptobot.config import DEFAULTS

NOW = 1_760_000_000.0  # fixed clock (seconds)


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(DEFAULTS)
    c.update(state_dir=str(tmp_path / "state"), log_dir=str(tmp_path / "logs"))
    return c


class FakeTelegram:
    chat_id = "42"

    def __init__(self):
        self.sent, self.inbox, self.prompts, self.edits, self.answers = [], [], {}, [], []
        self.keyboard_sent = False

    def updates(self, offset, timeout=25):
        return self.inbox.pop(0) if self.inbox else []

    def send(self, text, keyboard=False):
        self.sent.append(text)
        self.keyboard_sent |= keyboard
        return True

    def ask(self, text, token):
        mid = len(self.prompts) + 100
        self.prompts[mid] = (text, token)
        return mid

    def choose(self, text, options):
        mid = len(self.prompts) + 500
        self.prompts[mid] = (text, options)
        return mid

    def close_prompt(self, message_id, text):
        self.edits.append((message_id, text))

    def answer_button(self, callback_id, text=""):
        self.answers.append(text)

    def setup(self):
        pass

    def typing(self):
        pass

    def download(self, file_id, dest):
        dest.write_bytes(b"\x89PNG fake")
        return dest


class FakeExchange:
    """Two positions: BTC opened by the bot (state file), ETH manual without a stop."""

    def __init__(self):
        self.calls = []
        self.rejected = False

    def fetch_balance(self):
        self.calls.append("fetch_balance")
        if self.rejected:
            import ccxt
            raise ccxt.AuthenticationError('binance {"code":-2015,"msg":"Invalid API-key, IP, or permissions '
                                           'for action, request ip: 1.2.3.4"}')
        return {"info": {"totalMarginBalance": "392.5", "totalWalletBalance": "390", "availableBalance": "250.25",
                         "totalUnrealizedProfit": "2.5"}}

    def fetch_positions(self):
        self.calls.append("fetch_positions")
        return [
            {"symbol": "BTC/USDT:USDT", "side": "short", "contracts": 0.013, "entryPrice": 83556.1,
             "markPrice": 83500, "unrealizedPnl": 0.73, "liquidationPrice": 0, "leverage": 5, "marginMode": "cross"},
            {"symbol": "ETH/USDT:USDT", "side": "long", "contracts": 0.5, "entryPrice": 2500,
             "markPrice": 2510, "unrealizedPnl": 5, "liquidationPrice": 2000, "leverage": None, "marginMode": "isolated"},
            {"symbol": "SOL/USDT:USDT", "side": None, "contracts": 0, "entryPrice": 0},
        ]

    def fetch_open_orders(self, symbol, params=None):
        self.calls.append(("fetch_open_orders", symbol, dict(params or {})))
        if symbol == "BTC/USDT:USDT" and (params or {}).get("trigger"):
            return [{"id": "1", "type": "market", "side": "buy", "triggerPrice": 84670.1, "amount": None,
                     "reduceOnly": True, "info": {"orderType": "STOP_MARKET", "closePosition": True}},
                    {"id": "2", "type": "market", "side": "buy", "triggerPrice": 80000, "amount": 0.005,
                     "takeProfitPrice": 80000, "reduceOnly": True,
                     "info": {"orderType": "TAKE_PROFIT_MARKET", "closePosition": False}}]
        return []


def _bot_state(cfg):
    st = Path(cfg["state_dir"])
    st.mkdir(parents=True, exist_ok=True)
    (st / "binance_scan_live_BTC-USDT-USDT.json").write_text(json.dumps(
        {"symbol": "BTC/USDT:USDT", "position": {"side": "short"}, "pending": None}))
    (st / "binance_scan_live_ETH-USDT-USDT.json").write_text(json.dumps(
        {"symbol": "ETH/USDT:USDT", "position": None, "pending": None}))  # bot closed its ETH earlier


def _msg(text=None, chat=42, date=NOW, update_id=1, **extra):
    m = {"chat": {"id": chat}, "from": {"id": chat}, "date": date, **extra}
    if text is not None:
        m["text"] = text
    return {"update_id": update_id, "message": m}


class FakeAI:
    def __init__(self):
        self.asked, self.resets = [], 0
        self.inbox_dir = None

    def ask(self, text, image=None, snapshot=None):
        self.asked.append((text, image, snapshot))
        return "AI 回覆"

    def reset(self):
        self.resets += 1


def _butler(cfg, tmp_path, ai=None, **kw):
    tg, ex = FakeTelegram(), FakeExchange()
    b = Butler(cfg, tg, ex, ai=ai, repo_dir=str(tmp_path), audit_path=str(tmp_path / "logs" / "audit.log"),
               state_path=str(tmp_path / "state" / "butler.json"), service_state=lambda unit: "active",
               rejections=lambda: [], public_ip=lambda exc=None: "9.9.9.9", clock=lambda: NOW,
               inbox_dir=str(tmp_path / "inbox"), **kw)
    return b, tg, ex


# ------------------------------------------------------------------ messages
def test_long_replies_are_split_for_telegram():
    assert split_message("hi") == ["hi"]
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = split_message(text, limit=1000)
    assert len(parts) > 1 and all(len(p) <= 1000 + 12 for p in parts)
    assert parts[0].endswith(f"（1/{len(parts)}）")
    body = "\n".join(p.rsplit("\n", 1)[0] for p in parts)
    assert body.replace("\n", "") == text.replace("\n", "")  # nothing lost at the cuts
    assert len(split_message("y" * 2500, limit=1000)) == 3   # no line breaks: hard cut


# ------------------------------------------------------------------ /status
def test_status_lists_bot_and_manual_positions_with_stops_and_warns_without_stop(cfg, tmp_path):
    _bot_state(cfg)
    snap = account_snapshot(FakeExchange(), cfg)
    assert [p["owner"] for p in snap["positions"]] == ["bot", "manual"]
    assert snap["available"] == pytest.approx(250.25)
    btc = snap["positions"][0]
    assert [(o["kind"], o["amount"]) for o in btc["orders"]] == [("stop", None), ("tp", 0.005)]
    text = format_status(snap, "active")
    assert "帳戶總額 392.50 U" in text and "可用 250.25 U" in text and "未實現損益 +2.50 U" in text
    assert "🤖 BTC 空 0.013 5x 全倉 @ 83556.1 → 83500  +0.73 U" in text
    assert "停損 84670.1（全部）｜止盈 80000（0.005）" in text
    assert "✋ ETH 多 0.5 逐倉" in text and "⚠️ 沒有停損單：ETH" in text
    assert "交易機器人：運作中" in text


def test_status_command_reads_only_and_answers_when_the_bot_is_down(cfg, tmp_path):
    _bot_state(cfg)
    b, tg, ex = _butler(cfg, tmp_path)
    b.service_state = lambda unit: "failed"
    tg.inbox.append([_msg("/status")])
    b.poll()
    assert "即時狀態" in tg.sent[-1] and "⚠️ failed" in tg.sent[-1]
    read_only = {"fetch_balance", "fetch_positions", "fetch_open_orders"}
    assert all((c if isinstance(c, str) else c[0]) in read_only for c in ex.calls)
    ex.rejected = True
    b.cmd_status("")
    assert "幣安拒絕 API" in tg.sent[-1] and "/ip" in tg.sent[-1]


# ------------------------------------------------------------------ access control
def test_only_the_owner_is_answered_and_everything_is_audited(cfg, tmp_path):
    b, tg, _ = _butler(cfg, tmp_path, ai=FakeAI())
    tg.inbox.append([_msg("/status", chat=999, update_id=5), _msg("hello", chat=999, update_id=6),
                     _msg("/help", update_id=7)])
    b.poll()
    assert tg.sent == [HELP]
    assert b.ai.asked == []
    assert b.offset == 8 and json.loads(Path(b.state_path).read_text())["offset"] == 8
    audit = Path(b.audit_path).read_text(encoding="utf-8")
    assert audit.count("ignored chat=999") == 2 and "command /help" in audit


def test_old_messages_from_downtime_are_skipped_not_executed(cfg, tmp_path):
    b, tg, _ = _butler(cfg, tmp_path, ai=FakeAI())
    tg.inbox.append([_msg("/scan", date=NOW - 3600, update_id=1), _msg("/help", update_id=2)])
    b.poll()
    assert tg.sent[0] == HELP and "略過 1 則" in tg.sent[1]


def test_unknown_command_and_chinese_status_alias(cfg, tmp_path):
    b, tg, _ = _butler(cfg, tmp_path)
    tg.inbox.append([_msg("/daily", update_id=1), _msg("狀態", update_id=2)])
    b.poll()
    assert "不認得 /daily" in tg.sent[0] and "即時狀態" in tg.sent[1]


# ------------------------------------------------------------------ /ip and /scan
def test_ip_reports_rejection_and_recent_journal_hits(cfg, tmp_path):
    from cryptobot.runner import public_ip
    b, tg, ex = _butler(cfg, tmp_path)
    b.cmd_ip("")
    assert "9.9.9.9" in tg.sent[-1] and "連線正常" in tg.sent[-1] and "沒有被拒絕" in tg.sent[-1]
    ex.rejected = True
    b.public_ip = public_ip  # the IP Binance saw comes from the error message
    b.rejections = lambda: ["2026-10-11T09:26:32+0800 freqtrade01 python3[1]: ... -2015 ..."]
    b.cmd_ip("")
    assert "1.2.3.4" in tg.sent[-1] and "被拒 1 次" in tg.sent[-1] and "2026-10-11T09:26:32" in tg.sent[-1]


def test_scan_runs_the_existing_fib_scan(cfg, tmp_path):
    ran = []

    def run(cmd, **kw):
        ran.append((cmd, kw["cwd"]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    b, tg, _ = _butler(cfg, tmp_path, run=run)
    b._scan()
    assert ran[0][0][1:] == ["-m", "cryptobot", "fib-scan", "--send", "--margin", "10", "--leverage", "20"]
    assert ran[0][1] == str(tmp_path) and "完成" in tg.sent[-1]
    b.run_cmd = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom")
    b._scan()
    assert "失敗" in tg.sent[-1] and "boom" in tg.sent[-1]


# ------------------------------------------------------------------ AI routing
def test_text_and_photos_go_to_the_ai_with_a_fresh_snapshot(cfg, tmp_path):
    _bot_state(cfg)
    ai = FakeAI()
    b, tg, _ = _butler(cfg, tmp_path, ai=ai)
    tg.inbox.append([_msg("BTC 現在可以空嗎", update_id=1),
                     _msg(caption="看這張", photo=[{"file_id": "small", "file_size": 10},
                                                   {"file_id": "big", "file_size": 999}], update_id=2)])
    b.poll()
    assert b.jobs.qsize() == 2
    while not b.jobs.empty():
        text, photo = b.jobs.get()
        tg.send(b.answer(text, photo))
    (t1, img1, snap1), (t2, img2, _) = ai.asked
    assert t1 == "BTC 現在可以空嗎" and img1 is None and snap1["positions"][0]["owner"] == "bot"
    assert t2 == "看這張" and img2.exists() and img2.parent == tmp_path / "inbox"
    tg.inbox.append([_msg("/new", update_id=3)])
    b.poll()
    assert ai.resets == 1


def test_ai_disabled_without_credentials(cfg, tmp_path):
    b, tg, _ = _butler(cfg, tmp_path, ai=None)
    tg.inbox.append([_msg("hello")])
    b.poll()
    assert "尚未設定" in tg.sent[-1]


# ------------------------------------------------------------------ Claude Code call
class FakeClaude:
    def __init__(self):
        self.calls = []
        self.replies = []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = self.replies.pop(0) if self.replies else {"type": "result", "subtype": "success", "is_error": False,
                                                         "result": "好的", "session_id": f"s{len(self.calls)}"}
        return subprocess.CompletedProcess(cmd, 0, "warning line\n" + json.dumps(out), "")


def _chat(cfg, tmp_path, clock):
    repo = tmp_path / "repo"
    (repo / "deploy" / "ai").mkdir(parents=True)
    for name in ("settings.json", "system-prompt.md", "market.py"):
        (repo / "deploy" / "ai" / name).write_text(name)
    (repo / "config.yaml").write_text("symbol: BTC/USDT:USDT\n")
    (repo / "logs").mkdir()
    (repo / "logs" / "binance_scan_live_trades.csv").write_text("closed_at,pnl\n")
    (repo / "logs" / "butler_audit.log").write_text("private chat")
    (repo / ".env").write_text("EXCHANGE_API_SECRET=nope")
    cfg.update(state_dir="state", log_dir="logs")
    fake = FakeClaude()
    chat = ClaudeChat(cfg, str(repo), "/bin/claude", "TOKEN", root=str(tmp_path / "ai"),
                      tools_dir=str(tmp_path / "opt"), user=None, run=fake, clock=lambda: clock[0])
    chat.install()
    return chat, fake


def test_claude_is_called_read_only_with_a_minimal_environment(cfg, tmp_path):
    clock = [NOW]
    chat, fake = _chat(cfg, tmp_path, clock)
    snap = account_snapshot(FakeExchange(), {**cfg, "state_dir": str(tmp_path / "none")})
    assert chat.ask("分析 BTC", snapshot=snap) == "好的"
    # the current account goes into every prompt: a resumed chat never answers from an old snapshot
    assert "以這份為準" in fake.calls[0][0][2] and "停損 84670.1（全部）" in fake.calls[0][0][2]
    cmd, kw = fake.calls[0]
    assert cmd[:3] == ["/bin/claude", "-p", cmd[2]] and "分析 BTC" in cmd[2]
    assert cmd[cmd.index("--tools") + 1] == "Read,Bash"
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--strict-mcp-config" in cmd and "--resume" not in cmd
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert not any(k.startswith("EXCHANGE") or k.startswith("TELEGRAM") for k in kw["env"])
    assert kw["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "TOKEN" and kw["cwd"] == str(chat.cwd)
    assert str(chat.work_dir / "account.json") in cmd[2]
    work = sorted(p.relative_to(chat.work_dir).as_posix() for p in chat.work_dir.rglob("*") if p.is_file())
    assert work == ["account.json", "config.yaml", "logs/binance_scan_live_trades.csv"]  # no .env, no audit log
    assert (tmp_path / "opt" / "settings.json").read_text() == "settings.json"


def test_conversation_continues_then_restarts_after_two_idle_hours(cfg, tmp_path):
    clock = [NOW]
    chat, fake = _chat(cfg, tmp_path, clock)
    chat.ask("一", snapshot={})
    clock[0] += 3600
    chat.ask("二", snapshot={})
    assert fake.calls[1][0][-2:] == ["--resume", "s1"]
    clock[0] += 2 * 3600 + 1
    reply = chat.ask("三", snapshot={})
    assert "--resume" not in fake.calls[2][0] and reply.startswith("🆕")
    chat.reset()
    chat.ask("四", snapshot={})
    assert "--resume" not in fake.calls[3][0]


def test_image_path_is_handed_to_claude_and_errors_are_reported(cfg, tmp_path):
    clock = [NOW]
    chat, fake = _chat(cfg, tmp_path, clock)
    img = tmp_path / "ai" / "inbox" / "1.jpg"
    chat.ask("", image=img, snapshot={})
    assert str(img) in fake.calls[0][0][2] and "請分析這張圖" in fake.calls[0][0][2]
    fake.replies.append({"subtype": "error_max_turns", "is_error": True, "result": "", "session_id": "s9"})
    assert "太多" in chat.ask("x", snapshot={})
    fake.replies.append({"subtype": "error_during_execution", "is_error": True,
                         "result": "No conversation found with session ID", "session_id": None})
    assert chat.ask("y", snapshot={}) == "好的"  # lost session: retried as a new conversation
    assert "--resume" not in fake.calls[-1][0]


# ================================================================== phase 2: money operations
class TradingExchange(FakeExchange):
    """Stateful: positions and resting orders change when orders are sent."""

    def __init__(self):
        super().__init__()
        self.pos = {"BTC/USDT:USDT": {"side": "short", "contracts": 0.013, "entryPrice": 83556.1, "markPrice": 83500},
                    "ETH/USDT:USDT": {"side": "long", "contracts": 0.5, "entryPrice": 2500, "markPrice": 2510}}
        self.orders = {"BTC/USDT:USDT": [{"id": "old-stop", "trigger": True, "side": "buy", "triggerPrice": 84670.1,
                                          "amount": None, "reduceOnly": True,
                                          "info": {"orderType": "STOP_MARKET", "closePosition": True}}],
                       "ETH/USDT:USDT": []}
        self.fail_create = False
        self._n = 0

    def load_markets(self):
        return {}

    def amount_to_precision(self, symbol, amount):
        return f"{int(amount * 1000) / 1000:.3f}"

    def price_to_precision(self, symbol, price):
        return f"{price:.1f}"

    def fetch_positions(self, symbols=None):
        self.calls.append("fetch_positions")
        out = []
        for sym, p in self.pos.items():
            if symbols and sym not in symbols:
                continue
            out.append({"symbol": sym, "liquidationPrice": 0, "leverage": None, "marginMode": "cross",
                        "unrealizedPnl": (p["markPrice"] - p["entryPrice"]) * p["contracts"] * (1 if p["side"] == "long" else -1),
                        **p})
        return out

    def fetch_open_orders(self, symbol, params=None):
        self.calls.append(("fetch_open_orders", symbol, dict(params or {})))
        trigger = bool((params or {}).get("trigger"))
        return [o for o in self.orders.get(symbol, []) if o["trigger"] == trigger]

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        self.calls.append(("create_order", symbol, type_, side, amount, dict(params or {})))
        if self.fail_create:
            raise RuntimeError("binance -2021 Order would immediately trigger")
        self._n += 1
        params = params or {}
        oid = f"new-{self._n}"
        if "stopLossPrice" in params or "takeProfitPrice" in params:
            tp = "takeProfitPrice" in params
            self.orders[symbol].append({"id": oid, "trigger": True, "side": side, "amount": amount,
                                        "triggerPrice": params.get("takeProfitPrice") or params.get("stopLossPrice"),
                                        "takeProfitPrice": params.get("takeProfitPrice"), "reduceOnly": True,
                                        "info": {"orderType": "TAKE_PROFIT_MARKET" if tp else "STOP_MARKET",
                                                 "closePosition": False}})
            return {"id": oid}
        p = self.pos[symbol]  # reduce-only market close
        p["contracts"] = round(p["contracts"] - amount, 6)
        if p["contracts"] <= 0:
            del self.pos[symbol]
        return {"id": oid, "filled": amount, "average": p["markPrice"]}

    def fetch_order(self, oid, symbol):
        return {"id": oid, "filled": None}

    def fetch_ohlcv(self, symbol, tf, limit=100):
        """BTC: 4h range 83200-83800 (ATR 600) with a swing high 84200 and low 82000 inside the
        last 20 bars; daily range 83000-84000 with 86000 / 80000 in the last 10 days."""
        n = 60 if tf == "4h" else 30
        rows = [[i, 83500, 83800, 83200, 83500, 1] if tf == "4h" else [i, 83500, 84000, 83000, 83500, 1]
                for i in range(n)]
        if tf == "4h":
            rows[-18][2], rows[-17][3] = 84200, 82000
        else:
            rows[-5][2], rows[-5][3] = 86000, 80000
        return rows

    def cancel_order(self, oid, symbol, params=None):
        self.calls.append(("cancel_order", oid, dict(params or {})))
        self.orders[symbol] = [o for o in self.orders[symbol] if o["id"] != oid]


def _trading_butler(cfg, tmp_path, clock=None):
    _bot_state(cfg)
    clock = clock or [NOW]
    b, tg, _ = _butler(cfg, tmp_path)
    ex = TradingExchange()
    b.exchange = ex
    b.clock = lambda: clock[0]
    return b, tg, ex, clock


def _press(tg, b, verdict="ok", who=42, update_id=50):
    mid, (text, token) = list(tg.prompts.items())[-1]
    tg.inbox.append([{"update_id": update_id, "callback_query": {
        "id": "cb", "from": {"id": who}, "data": f"{verdict}:{token}",
        "message": {"message_id": mid, "chat": {"id": 42}}}}])
    b.poll()


def _sent_orders(ex):
    return [c for c in ex.calls if isinstance(c, tuple) and c[0] in ("create_order", "cancel_order")]


def test_stop_is_shown_first_then_placed_before_the_old_one_is_cancelled(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    tg.inbox.append([_msg("/stop btc 84000")])
    b.poll()
    text, _ = list(tg.prompts.values())[-1]
    assert "移動停損" in text and "BTC 空單（🤖 機器人）" in text and "整個部位 0.013" in text
    assert "-5.77 U" in text and "84670.1" in text and "交易機器人開的倉" in text and "60 秒" in text
    assert _sent_orders(ex) == []                      # nothing sent before ✅
    _press(tg, b)
    assert _sent_orders(ex) == [
        ("create_order", "BTC/USDT:USDT", "market", "buy", 0.013, {"stopLossPrice": 84000.0, "reduceOnly": True}),
        ("cancel_order", "old-stop", {"trigger": True})]
    assert "新停損已掛上 84000" in tg.sent[-1] and "停損 84000（0.013）" in tg.sent[-1]  # read back
    assert "已確認" in tg.edits[-1][1]
    audit = Path(b.audit_path).read_text(encoding="utf-8")
    assert "propose" in audit and "confirmed" in audit and "result" in audit


def _pick(tg, b, n, update_id=70):
    mid, (text, options) = list(tg.prompts.items())[-1]
    tg.inbox.append([{"update_id": update_id, "callback_query": {
        "id": "cb", "from": {"id": 42}, "data": options[n - 1][1],
        "message": {"message_id": mid, "chat": {"id": 42}}}}])
    b.poll()


def test_stop_without_price_lists_levels_then_confirms_the_picked_one(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    tg.inbox.append([_msg("/stop BTC")])
    b.poll()
    text, options = list(tg.prompts.values())[-1]
    assert "建議停損" in text and "目前停損 84670.1" in text
    assert [o[0] for o in options] == ["1. 停損 83556.1", "2. 停損 84367", "3. 停損 86167"]
    assert "保本" in text and "4h 近 20 根波段高點上方" in text and "日線近 10 天高點上方" in text
    assert "ATR" in text and _sent_orders(ex) == []
    _pick(tg, b, 2)
    assert "已選 2. 停損 84367" in tg.edits[-1][1] and b.pending and _sent_orders(ex) == []  # picking alone sends nothing
    text, _ = list(tg.prompts.values())[-1]
    assert "新停損 84367" in text
    _press(tg, b, update_id=71)
    assert _sent_orders(ex)[0] == ("create_order", "BTC/USDT:USDT", "market", "buy", 0.013,
                                   {"stopLossPrice": 84367.0, "reduceOnly": True})


def test_everything_by_tapping_from_the_reply_keyboard(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    tg.inbox.append([_msg("🛑 停損")])
    b.poll()
    text, options = list(tg.prompts.values())[-1]
    assert "哪一個的停損" in text and [o[0] for o in options] == ["🤖 BTC 空　+0.73 U", "✋ ETH 多　+5.00 U"]
    _pick(tg, b, 1, update_id=90)                       # BTC -> its suggested stops
    assert "建議停損：BTC" in list(tg.prompts.values())[-1][0]
    _pick(tg, b, 2, update_id=91)                       # a level -> the usual confirmation
    assert "新停損 84367" in list(tg.prompts.values())[-1][0] and _sent_orders(ex) == []

    tg.inbox.append([_msg("💥 平倉", update_id=92)])
    b.poll()
    _pick(tg, b, 2, update_id=93)                       # ETH -> how much
    text, options = list(tg.prompts.values())[-1]
    assert "ETH 要平掉多少" in text and options[1][0] == "50%（約 +2.50 U）"
    _pick(tg, b, 2, update_id=94)
    assert "部分平倉" in list(tg.prompts.values())[-1][0]
    _press(tg, b, update_id=95)
    assert ("create_order", "ETH/USDT:USDT", "market", "sell", 0.25, {"reduceOnly": True}) in ex.calls

    ex.pos.clear()
    tg.inbox.append([_msg("🎯 止盈", update_id=96)])
    b.poll()
    assert tg.sent[-1] == "目前沒有持倉"


def test_tp_without_price_offers_structure_and_r_targets(cfg, tmp_path):
    b, tg, ex, clock = _trading_butler(cfg, tmp_path)
    b.cmd_tp("BTC")
    text, options = list(tg.prompts.values())[-1]
    assert [o[0] for o in options] == ["1. 止盈 82442.1", "2. 止盈 82000", "3. 止盈 81328.1", "4. 止盈 80000"]
    assert "1R 目標（以目前停損算 R）" in text and "2R 目標" in text and "+14.48 U" in text
    clock[0] += 121
    b.poll()                                            # the whole list expires together
    assert b.pending == {} and "作廢" in tg.edits[-1][1]
    b.cmd_tp("BTC")                                     # pick a level, then how much to take off
    _pick(tg, b, 4, update_id=80)
    text, options = list(tg.prompts.values())[-1]
    assert "要平掉多少" in text and [o[0] for o in options] == [
        "25%（約 0.00325）", "50%（約 0.0065）", "75%（約 0.00975）", "100%（約 0.013）"]
    _pick(tg, b, 2, update_id=81)
    assert "止盈 80000：數量 0.0065（50%）" in list(tg.prompts.values())[-1][0] and _sent_orders(ex) == []
    b.cmd_tp("BTC 80000")                               # typed without an amount: the same choice
    assert "要平掉多少" in list(tg.prompts.values())[-1][0]
    from cryptobot.manage import OpError
    with pytest.raises(OpError, match="要低於現價"):  # wrong side is refused before asking
        b.cmd_tp("BTC 85000")


def test_stop_on_the_wrong_side_or_unknown_coin_is_refused(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    tg.inbox.append([_msg("/stop BTC 83000", update_id=1), _msg("/stop SOL 100", update_id=2),
                     _msg("/stop BTC 1 2", update_id=3), _msg("/stop BTC abc", update_id=4)])
    b.poll()
    assert "要高於現價" in tg.sent[0] and "SOL 沒有持倉" in tg.sent[1]
    assert "用法" in tg.sent[2] and "看不懂價格" in tg.sent[3]
    assert tg.prompts == {} and _sent_orders(ex) == []


def test_leftover_stop_from_an_opposite_position_is_flagged_and_replaced(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    ex.orders["ETH/USDT:USDT"].append({"id": "short-left", "trigger": True, "side": "buy", "triggerPrice": 2600,
                                       "amount": 0.3, "reduceOnly": True,
                                       "info": {"orderType": "STOP_MARKET", "closePosition": False}})
    b.cmd_status("")
    assert "停損 —" in tg.sent[-1] and "方向不對的殘單" in tg.sent[-1] and "沒有停損單：ETH" in tg.sent[-1]
    b.cmd_stop("ETH 2400")
    assert "也會取消方向不對的殘單：2600" in list(tg.prompts.values())[-1][0]
    _press(tg, b)
    assert [c[0] for c in _sent_orders(ex)] == ["create_order", "cancel_order"]
    assert ex.orders["ETH/USDT:USDT"][0]["side"] == "sell"


def test_failed_new_stop_keeps_the_old_one(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    b.cmd_stop("BTC 84000")
    ex.fail_create = True
    _press(tg, b)
    assert [c[0] for c in _sent_orders(ex)] == ["create_order"]   # old stop never cancelled
    assert "執行失敗" in tg.sent[-1] and ex.orders["BTC/USDT:USDT"][0]["id"] == "old-stop"


def test_unconfirmed_operations_expire_and_cancel_or_strangers_do_nothing(cfg, tmp_path):
    b, tg, ex, clock = _trading_butler(cfg, tmp_path)
    b.cmd_close("ETH")
    _press(tg, b, who=999, update_id=1)                 # someone else pressing ✅
    assert _sent_orders(ex) == [] and len(b.pending) == 1
    _press(tg, b, verdict="no", update_id=2)
    assert _sent_orders(ex) == [] and "已取消" in tg.edits[-1][1] and b.pending == {}

    b.cmd_close("ETH")
    clock[0] += 61
    b.poll()                                            # swept: prompt edited, op dropped
    assert "作廢" in tg.edits[-1][1] and b.pending == {}
    _press(tg, b, update_id=3)                          # a late ✅
    assert "失效" in tg.answers[-1] and _sent_orders(ex) == []


def test_take_profit_amount_and_validation(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    b.cmd_tp("ETH 2600 50%")
    text, _ = list(tg.prompts.values())[-1]
    assert "數量 0.25（50%）" in text and "+25.00 U" in text and "交易機器人" not in text
    _press(tg, b)
    assert _sent_orders(ex) == [("create_order", "ETH/USDT:USDT", "market", "sell", 0.25,
                                 {"takeProfitPrice": 2600.0, "reduceOnly": True})]
    assert "止盈已掛上 2600" in tg.sent[-1] and "止盈 2600（0.25）" in tg.sent[-1]
    for bad, why in (("ETH 2400 50%", "要高於現價"), ("ETH 2600 1", "超過持倉"), ("ETH 2600 50% x", "用法")):
        tg.inbox.append([_msg(f"/tp {bad}", update_id=60)])
        b.poll()
        assert why in tg.sent[-1]


def test_partial_then_full_close_cancels_leftover_orders(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    b.cmd_close("BTC 50%")
    assert "部分平倉" in list(tg.prompts.values())[-1][0]
    _press(tg, b, update_id=1)
    assert ex.pos["BTC/USDT:USDT"]["contracts"] == pytest.approx(0.007)  # 0.0065 rounded by precision
    assert ("create_order", "BTC/USDT:USDT", "market", "buy", 0.006, {"reduceOnly": True}) in ex.calls
    assert "持倉 0.007" in tg.sent[-1]
    b.cmd_close("BTC")
    text = list(tg.prompts.values())[-1][0]
    assert "全部平倉" in text and "取消 BTC 剩下的 1 張" in text
    _press(tg, b, update_id=2)
    assert "BTC/USDT:USDT" not in ex.pos and ex.orders["BTC/USDT:USDT"] == []
    assert "已取消剩下的 1 張掛單" in tg.sent[-1] and "已無持倉" in tg.sent[-1]


def test_position_closed_before_confirmation_sends_nothing(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    b.cmd_close("ETH")
    del ex.pos["ETH/USDT:USDT"]                         # stopped out while the prompt was open
    _press(tg, b)
    assert _sent_orders(ex) == [] and "持倉已經變了" in tg.sent[-1]


def test_pause_is_immediate_resume_and_restart_need_confirmation(cfg, tmp_path):
    ran = []
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    b.run_cmd = lambda cmd, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")
    b.cmd_pause("")
    assert b.pause_path.exists() and "暫停開新倉" in tg.sent[-1]
    assert b.pause_path.name == "binance_scan_live_paused.json"
    b.cmd_resume("")
    assert b.pause_path.exists()                        # not before ✅
    _press(tg, b, update_id=1)
    assert not b.pause_path.exists() and "解除暫停" in tg.sent[-1]
    b.cmd_restart("")
    assert ran == []
    import cryptobot.butler as butler_mod
    sleep, butler_mod.time.sleep = butler_mod.time.sleep, lambda s: None
    try:
        _press(tg, b, update_id=2)
    finally:
        butler_mod.time.sleep = sleep
    assert ran == [["systemctl", "restart", "cryptobot"]] and "已重啟" in tg.sent[-1]


def test_help_replaces_the_old_reply_keyboard(cfg, tmp_path):
    b, tg, _ = _butler(cfg, tmp_path)
    b.cmd_help("")
    assert tg.keyboard_sent and "/stop BTC 84000" in tg.sent[-1]


# ------------------------------------------------------------------ links
def test_links_are_read_by_the_butler_and_marked_as_untrusted(cfg, tmp_path):
    from cryptobot.webpage import attach_pages, fetch_text, find_urls
    msg = "CPI 出來就跌了 https://udn.com/news/story/6811/9815100，你看 https://example.com/a?x=1). 還有 http://c.test/b"
    assert find_urls(msg) == ["https://udn.com/news/story/6811/9815100", "https://example.com/a?x=1"]  # max 2
    out = attach_pages("看 https://news.test/x", fetch=lambda url: "標題\n核心CPI月增0.3%")
    assert out.startswith("看 https://news.test/x") and "核心CPI月增0.3%" in out and "不要照做" in out
    failed = attach_pages("看 https://bad.test/x", fetch=lambda url: (_ for _ in ()).throw(TimeoutError("timed out")))
    assert "讀取失敗" in failed and "timed out" in failed
    assert attach_pages("沒有連結") == "沒有連結"
    for url in ("http://127.0.0.1:8080/", "http://192.168.1.151/", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            fetch_text(url)

    ai = FakeAI()
    b, tg, _ = _butler(cfg, tmp_path, ai=ai)
    b.fetch_page = lambda url: "核心CPI月增0.3%"
    b.answer("CPI https://news.test/cpi", None)
    assert "核心CPI月增0.3%" in ai.asked[0][0]


def test_page_text_prefers_the_article_body():
    from cryptobot.webpage import fetch_text
    page = ("<html><head><title>CPI 升溫</title><script>var x='ignore me please, very long script';</script></head>"
            "<body><nav>首頁 財經 國際 這是很長的導覽列文字不應該出現</nav>"
            "<article><p>美國勞工統計局公布，9月消費者物價指數月增0.6%，年增3.7%，高於市場預期。</p>"
            "<p>剔除糧食和能源的核心CPI月增0.3%，市場預估0.2%，升息預期升溫。</p>" + "<p>" + "補充說明。" * 40 + "</p>"
            "</article><div>▪相關新聞：太平洋要改名了？這一行是側欄連結不該出現</div></body></html>").encode()

    class Resp:
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return page

        def geturl(self):
            return "https://example.com/news"

    text = fetch_text("https://example.com/news", opener=lambda req, timeout: Resp())
    assert text.startswith("CPI 升溫") and "核心CPI月增0.3%" in text
    assert "導覽列" not in text and "側欄" not in text and "ignore me" not in text


# ------------------------------------------------------------------ /pnl
def _fill(t, side, qty, realized=0.0, fee=0.1, client="ios_x", fid=None):
    return {"symbol": "BTCUSDT", "time": t, "id": fid or t, "side": side, "qty": qty,
            "realized": realized, "commission": fee, "client": client}


def test_round_trips_rebuilt_back_from_the_current_position():
    from cryptobot.pnl import build_trips
    fills = [_fill(1, "BUY", 1.0, -5),           # closes a short from before the history: skipped
             _fill(2, "SELL", 2.0), _fill(3, "BUY", 1.0, 10), _fill(4, "BUY", 1.0, 8),   # short 2 -> flat
             _fill(5, "BUY", 3.0), _fill(6, "SELL", 4.0, 6),                             # long 3, flips short 1
             _fill(7, "SELL", 0.5)]                                                       # still short 1.5
    trips = build_trips(fills, position=-1.5)
    assert [(t.side, t.opened, t.closed) for t in trips] == [("short", 2, 4), ("long", 5, 6), ("short", 6, None)]
    assert trips[0].pnl == pytest.approx(18 - 0.3) and trips[1].pnl == pytest.approx(6 - 0.2)
    assert trips[2].pnl == pytest.approx(-0.1)


def test_trips_are_split_into_bot_and_manual():
    from cryptobot.pnl import Trip, classify
    windows = [("BTCUSDT", 1000, 5000, -1.0), (None, 9000, 9500, -1.0)]  # old journal row without a coin
    bot = Trip("BTCUSDT", "long", 1000, 5000, fills=[_fill(1000, "BUY", 1, client="x-cvBPrNm9a"),
                                                       _fill(5000, "SELL", 1, client="x-cvBPrNm9b")])
    app_same_time = Trip("BTCUSDT", "long", 2000, 3000, fills=[_fill(2000, "BUY", 1), _fill(3000, "SELL", 1)])
    script_later = Trip("BTCUSDT", "long", 600000, 700000, fills=[_fill(600000, "BUY", 1, client="x-cvBPrNm9c"),
                                                                   _fill(700000, "SELL", 1, client="x-cvBPrNm9d")])
    old_row = Trip("ONDOUSDT", "short", 9000, 9500, fills=[_fill(9000, "SELL", 1, client="x-cvBPrNm9e"),
                                                           _fill(9500, "BUY", 1, client="x-cvBPrNm9f")])
    for t in (bot, app_same_time, script_later, old_row):
        classify(t, windows)
    assert [t.owner for t in (bot, app_same_time, script_later, old_row)] == ["bot", "manual", "manual", "bot"]


def test_pnl_report_from_exchange_records(cfg, tmp_path):
    from cryptobot.pnl import collect, format_pnl
    day = 86_400_000
    now = 100 * day
    logs = Path(cfg["log_dir"])
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "binance_scan_live_trades.csv").write_text(
        "symbol,opened_at,closed_at,pnl,r_multiple\n"
        f"ETH/USDT:USDT,{pd_ts(now - 5 * day)},{pd_ts(now - 4 * day)},-10,-1.0\n")
    fills = {"ETHUSDT": [{"time": now - 5 * day, "id": 1, "orderId": 11, "side": "SELL", "qty": "1", "realizedPnl": "0", "commission": "0.5"},
                         {"time": now - 4 * day, "id": 2, "orderId": 12, "side": "BUY", "qty": "1", "realizedPnl": "-9", "commission": "0.5"}],
             "BTCUSDT": [{"time": now - 3 * day, "id": 3, "orderId": 13, "side": "BUY", "qty": "1", "realizedPnl": "0", "commission": "1"},
                         {"time": now - 2 * day, "id": 4, "orderId": 14, "side": "SELL", "qty": "1", "realizedPnl": "30", "commission": "1"}]}
    clients = {11: "x-cvBPrNm9a", 12: "x-cvBPrNm9b", 13: "ios_a", 14: "ios_b"}

    class Ex:
        def fapiPrivateGetIncome(self, p):
            rows = [{"symbol": s, "incomeType": "COMMISSION", "income": f"-{f['commission']}", "time": f["time"], "asset": "USDT"}
                    for s, fs in fills.items() for f in fs]
            rows.append({"symbol": "", "incomeType": "TRANSFER", "income": "100", "time": now - day, "asset": "USDT"})
            rows.append({"symbol": "ETHUSDT", "incomeType": "FUNDING_FEE", "income": "-0.25", "time": now - 4.5 * day, "asset": "USDT"})
            return [r for r in rows if p["startTime"] <= r["time"] <= p["endTime"]]

        def fapiPrivateGetUserTrades(self, p):
            return [f for f in fills[p["symbol"]] if p["startTime"] <= f["time"] <= p["endTime"]]

        def fapiPrivateGetAllOrders(self, p):
            return [{"orderId": f["orderId"], "clientOrderId": clients[f["orderId"]]} for f in self.fapiPrivateGetUserTrades(p)]

        def fetch_positions(self):
            return []

    text = format_pnl(collect(Ex(), cfg, 30, now_ms=now))
    assert "🤖 機器人：1 筆，賺 0 筆（0%），合計 -10.25 U" in text and "-1.00R" in text
    assert "✋ 手動：1 筆，賺 1 筆（100%），合計 +28.00 U" in text
    assert "轉入轉出 +100.00 U" in text


def pd_ts(ms):
    import pandas as pd
    return pd.Timestamp(ms, unit="ms", tz="UTC").isoformat()


def test_paused_scanner_opens_nothing_and_stops_adds(cfg, tmp_path):
    from cryptobot.backtest import make_paper_broker
    from cryptobot.scanner import Scanner
    from cryptobot.strategy import ENTER, TREND, Decision

    sent = []
    cfg["state_dir"] = str(tmp_path / "state")
    sc = Scanner(cfg, lambda s: make_paper_broker(cfg), state_dir=cfg["state_dir"], notify=sent.append, mode="live")
    t = sc.trader("BTC/USDT:USDT")
    tried = []
    t.try_enter = lambda *a: tried.append(a) or True
    sc._candidates = [(1.0, t, Decision(ENTER, TREND, "t", side="long", stop_price=90.0), 100.0)]
    sc.pause_path().parent.mkdir(parents=True, exist_ok=True)
    sc.pause_path().write_text("{}")
    assert sc.finish_candle(None) == [] and tried == [] and t.paused
    assert any("暫停開新倉" in m for m in sent)
    sc.pause_path().unlink()
    sc._candidates = [(1.0, t, Decision(ENTER, TREND, "t", side="long", stop_price=90.0), 100.0)]
    assert sc.finish_candle(None) == ["BTC/USDT:USDT"] and not t.paused
    assert "恢復開新倉" in sent[-1]
