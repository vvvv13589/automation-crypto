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
    assert chat.ask("分析 BTC", snapshot={"time": "t", "positions": []}) == "好的"
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


def test_stop_on_the_wrong_side_or_unknown_coin_is_refused(cfg, tmp_path):
    b, tg, ex, _ = _trading_butler(cfg, tmp_path)
    tg.inbox.append([_msg("/stop BTC 83000", update_id=1), _msg("/stop SOL 100", update_id=2),
                     _msg("/stop BTC", update_id=3), _msg("/stop BTC abc", update_id=4)])
    b.poll()
    assert "要高於現價" in tg.sent[0] and "SOL 沒有持倉" in tg.sent[1]
    assert "用法" in tg.sent[2] and "看不懂價格" in tg.sent[3]
    assert tg.prompts == {} and _sent_orders(ex) == []


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
    for bad, why in (("ETH 2400 50%", "要高於現價"), ("ETH 2600 1", "超過持倉"), ("ETH 2600", "用法")):
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
