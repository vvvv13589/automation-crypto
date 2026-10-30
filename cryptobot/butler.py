"""Telegram butler: the only process that reads the chat (getUpdates).

    python -m cryptobot butler          # systemd: deploy/cryptobot-telegram.service

Runs independently of the trading bot so it still answers when the bot is stopped or
crashed. Only messages from TELEGRAM_CHAT_ID are handled; every command is written to the
audit log. Read-only: /help /status /ip /scan /new; plain text or photos go to the AI
(cryptobot/ai.py), which only ever sees a snapshot of the account. Money operations (/stop /tp
/close, cryptobot/manage.py) and bot control (/resume /restart) run only after a ✅ button
press within 60 seconds; /pause (only lowers risk) runs at once.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .scanner import tag_for

log = logging.getLogger(__name__)

TG_LIMIT = 4000  # Telegram allows 4096 characters per message; leave room for a part marker
STALE_SECONDS = 600  # messages older than this (sent while the butler was down) are skipped
SCAN_ARGS = ["fib-scan", "--send", "--margin", "10", "--leverage", "20"]  # same as cryptobot-fibscan

AITRADER_ENV = "/etc/cryptobot-aitrader/live.env"  # the AI sub-account key (root 600)
CONFIRM_SECONDS = 60  # a money operation not confirmed within this is void
CHOICE_SECONDS = 120  # suggested stop / take-profit levels to pick from

HELP = """🤵 Telegram 管家指令
📊 查看
/status　主帳戶（總額、可用、持倉🤖/✋、停損止盈、損益）＋ AI 子帳戶、條件單、最後決策，一次看完
/money（或 /錢）　所有帳戶加起來多少：合約、資金錢包、現貨、理財、AI 子帳戶、預測下注，和今天／7 天的變化
/ipVM 對外 IP、幣安 API 是否接受連線、最近 24 小時有沒有 -2015 拒絕
/scan　立刻跑一次回撤掃描（和每天 09:00 的一樣）
/pnl　機器人與手動各自的已實現損益、勝率、盈虧比（最近 30 天；/pnl 7 看 7 天）

💰 管理部位（先顯示內容，按 ✅ 確認才下單，60 秒沒按就作廢）
點下方「🛑 停損」「🎯 止盈」「💥 平倉」→ 點持倉 → 點建議價格／數量 → ✅（全程點擊）
也可以直接打指令：
/stop BTC 84000　掛上或移動停損：整個部位、只減倉，先掛新的再取消舊的
/tp BTC 80000 50%　掛止盈：只減倉，數量可寫 50% 或幣數（例如 0.005），不寫會讓你選 25～100%
/stop BTC、/tp BTC　不寫價格 → 列出建議點位（波段高低點、ATR、保本、1R／2R），點選後再確認
/close BTC　市價全部平倉（並取消該幣剩下的停損止盈）
/close BTC 50%　市價平掉一半

🤖 交易機器人
/pause　暫停開新倉（現有持倉照常管理）
/resume　恢復開新倉
/restart　重啟交易機器人

💬 AI
/new　開新對話（超過 2 小時沒講話也會自動開新的）
直接打字或傳 K 線截圖 → AI 分析（唯讀，不能下單或改任何東西）
訊息裡貼新聞連結，管家會代為讀取網頁文字給 AI 參考（每則最多 2 個連結）"""

COMMANDS = [("status", "主帳戶＋AI 子帳戶 總覽"), ("money", "所有帳戶加起來的總資產"), ("pnl", "機器人 vs 手動 已實現損益"), ("ip", "對外 IP 與 API 狀態"),
            ("scan", "回撤掃描"),
            ("stop", "掛／移動停損：/stop BTC 84000（不寫價格=建議）"),
            ("tp", "掛止盈：/tp BTC 80000 50%（不寫價格=建議）"),
            ("close", "市價平倉：/close BTC 或 /close BTC 50%"), ("pause", "暫停開新倉"),
            ("resume", "恢復開新倉"), ("restart", "重啟交易機器人"), ("new", "AI 開新對話"), ("help", "指令說明")]
KEYBOARD = [["🛑 停損", "🎯 止盈", "💥 平倉"], ["/status", "/pnl", "/help"]]  # replaces any older reply keyboard
KEYBOARD_VERSION = 4  # bump to send the keyboard again after changing it
BUTTONS = {"🛑 停損": "/stop", "🎯 止盈": "/tp", "💥 平倉": "/close"}  # reply-keyboard text -> command


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Telegram-sized parts, cut at line breaks where possible."""
    text = text.strip() or "（空白回覆）"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    parts.append(text)
    if len(parts) > 1:
        parts = [f"{p}\n（{i}/{len(parts)}）" for i, p in enumerate(parts, 1)]
    return parts


class TelegramBot:
    """Bot API client for the butler: long-polls updates, sends (split) messages, downloads photos."""

    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.files = f"https://api.telegram.org/file/bot{token}"
        self.chat_id = str(chat_id)

    def setup(self) -> None:
        """The "/" command menu in the chat."""
        try:
            self._call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        except Exception as exc:
            log.warning("setMyCommands failed: %s", exc)

    def _call(self, method: str, params: dict | None = None, timeout: float = 15):
        body = json.dumps(params or {}).encode()
        req = urllib.request.Request(f"{self.api}/{method}", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    def updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=timeout + 10)

    def send(self, text: str, keyboard: bool = False) -> bool:
        """Split into Telegram-sized parts; ``keyboard`` also (re)sets the reply keyboard."""
        ok = True
        for part in split_message(text):
            params = {"chat_id": self.chat_id, "text": part}
            if keyboard:
                params["reply_markup"] = {"keyboard": KEYBOARD, "resize_keyboard": True, "is_persistent": True}
            try:
                self._call("sendMessage", params)
            except Exception as exc:
                log.warning("telegram send failed: %s", exc)
                ok = False
        return ok

    def ask(self, text: str, token: str) -> int | None:
        """A message with ✅ / ❌ buttons; returns its message id (None if it could not be sent)."""
        buttons = {"inline_keyboard": [[{"text": "✅ 確認", "callback_data": f"ok:{token}"},
                                        {"text": "❌ 取消", "callback_data": f"no:{token}"}]]}
        try:
            return self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:TG_LIMIT],
                                              "reply_markup": buttons})["message_id"]
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            return None

    def choose(self, text: str, options: list[tuple[str, str]]) -> int | None:
        """A message with one button per option (label, callback data), one per row."""
        buttons = {"inline_keyboard": [[{"text": label, "callback_data": data}] for label, data in options]}
        try:
            return self._call("sendMessage", {"chat_id": self.chat_id, "text": text[:TG_LIMIT],
                                              "reply_markup": buttons})["message_id"]
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            return None

    def close_prompt(self, message_id: int, text: str) -> None:
        """Replace a confirmation prompt's text and remove its buttons."""
        try:
            self._call("editMessageText", {"chat_id": self.chat_id, "message_id": message_id, "text": text[:TG_LIMIT]})
        except Exception as exc:
            log.warning("telegram edit failed: %s", exc)

    def answer_button(self, callback_id: str, text: str = "") -> None:
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, timeout=5)
        except Exception:
            pass

    def typing(self) -> None:
        try:
            self._call("sendChatAction", {"chat_id": self.chat_id, "action": "typing"}, timeout=5)
        except Exception:
            pass

    def download(self, file_id: str, dest: Path) -> Path:
        info = self._call("getFile", {"file_id": file_id})
        url = f"{self.files}/{urllib.parse.quote(info['file_path'])}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            dest.write_bytes(resp.read())
        return dest


# ------------------------------------------------------------------ account
def bot_symbols(cfg: dict) -> set[str]:
    """Symbols where the trading bot holds a position or a working entry (its state files)."""
    out = set()
    tag = tag_for(cfg, "live")
    for f in glob.glob(str(Path(cfg["state_dir"]) / f"{tag}_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            continue
        if st.get("symbol") and (st.get("position") or st.get("pending")):
            out.add(st["symbol"])
    return out


def _order_kind(o: dict) -> str:
    info = o.get("info") or {}
    t = str(info.get("orderType") or info.get("type") or o.get("type") or "").upper()
    if "TAKE_PROFIT" in t or o.get("takeProfitPrice"):
        return "tp"
    if t.startswith("STOP") or "TRAILING" in t or o.get("stopLossPrice"):
        return "stop"
    return "tp" if o.get("reduceOnly") else "order"


def _order(o: dict) -> dict:
    info = o.get("info") or {}
    close_all = str(info.get("closePosition")).lower() == "true"
    price = o.get("triggerPrice") or o.get("stopPrice") or o.get("price")
    return {"id": str(o.get("id")), "kind": _order_kind(o), "side": o.get("side"),
            "price": float(price or 0), "amount": None if close_all else float(o.get("amount") or 0),
            "close_position": close_all, "reduce_only": bool(o.get("reduceOnly")),
            "type": str(info.get("orderType") or info.get("type") or o.get("type") or "")}


def mark_leftovers(orders: list[dict], position_side: str) -> list[dict]:
    """Reduce-only stops / targets on the opening side (e.g. a buy stop left over from a short
    after flipping long) can never fill: show them as leftovers, not as protection."""
    closing = "sell" if position_side == "long" else "buy"
    for o in orders:
        if o["kind"] in ("stop", "tp") and o.get("side") and o["side"] != closing:
            o["kind"] = "leftover"
    return orders


def account_snapshot(exchange, cfg: dict) -> dict:
    """Balance, every open position (bot or manual) and its resting orders. Read-only calls."""
    info = exchange.fetch_balance().get("info") or {}
    mine = bot_symbols(cfg)
    positions = []
    for p in exchange.fetch_positions():
        if not float(p.get("contracts") or 0):
            continue
        sym = p["symbol"]
        orders = []
        for params in ({}, {"trigger": True}):  # plain limit orders, then conditional (algo) orders
            try:
                orders += [_order(o) for o in exchange.fetch_open_orders(sym, params=params)]
            except Exception as exc:
                log.warning("%s open orders %s unavailable: %s", sym, params, str(exc)[:120])
        positions.append({
            "symbol": sym, "coin": sym.split("/")[0], "owner": "bot" if sym in mine else "manual",
            "side": p.get("side"), "amount": float(p.get("contracts") or 0),
            "entry": float(p.get("entryPrice") or 0), "mark": float(p.get("markPrice") or 0),
            "unrealized": float(p.get("unrealizedPnl") or 0),
            "liquidation": float(p.get("liquidationPrice") or 0),
            "leverage": p.get("leverage"), "margin_mode": p.get("marginMode"),
            "orders": mark_leftovers(orders, p.get("side")),
        })
    return {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "equity": float(info.get("totalMarginBalance") or 0),
        "wallet": float(info.get("totalWalletBalance") or 0),
        "available": float(info.get("availableBalance") or 0),
        "unrealized": float(info.get("totalUnrealizedProfit") or 0),
        "positions": positions,
    }


def _px(x: float) -> str:
    return f"{x:.6g}"


def _orders_line(orders: list[dict], kind: str) -> str:
    picked = [o for o in orders if o["kind"] == kind]
    if not picked:
        return "—"
    return "、".join(f"{_px(o['price'])}（{'全部' if o['amount'] is None else _px(o['amount'])}）"
                    for o in sorted(picked, key=lambda o: o["price"]))


def format_status(snap: dict, bot_state: str = "", tz: str = "Asia/Taipei") -> str:
    now = pd.Timestamp(snap["time"]).tz_convert(tz)
    lines = [f"📊 即時狀態 {now:%m/%d %H:%M}",
             f"💰 帳戶總額 {snap['equity']:.2f} U｜可用 {snap['available']:.2f} U",
             f"📈 未實現損益 {snap['unrealized']:+.2f} U"]
    if bot_state:
        shown = {"active": "運作中", "inactive": "已停用"}.get(bot_state, f"⚠️ {bot_state}")
        lines.append(f"🤖 交易機器人：{shown}")
    pos = snap["positions"]
    if not pos:
        lines.append("📌 目前沒有持倉")
        return "\n".join(lines)
    lines.append(f"📌 持倉 {len(pos)} 筆（🤖 機器人／✋ 手動）")
    missing = []
    for p in pos:
        side = "多" if p["side"] == "long" else "空"
        lev = f" {int(float(p['leverage']))}x" if p.get("leverage") else ""
        mode = {"cross": " 全倉", "isolated": " 逐倉"}.get(p.get("margin_mode") or "", "")
        lines.append(f"{'🤖' if p['owner'] == 'bot' else '✋'} {p['coin']} {side} {_px(p['amount'])}{lev}{mode}"
                     f" @ {_px(p['entry'])} → {_px(p['mark'])}  {p['unrealized']:+.2f} U")
        lines.append(f"🎯 停損 {_orders_line(p['orders'], 'stop')}｜止盈 {_orders_line(p['orders'], 'tp')}")
        if any(o["kind"] == "leftover" for o in p["orders"]):
            lines.append(f"⚠️ 方向不對的殘單（不會成交，可在幣安取消）：{_orders_line(p['orders'], 'leftover')}")
        if not any(o["kind"] == "stop" for o in p["orders"]):
            missing.append(p["coin"])
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    if missing:
        lines.append("")
        lines.append(f"⚠️ 沒有停損單：{', '.join(missing)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- butler
def systemctl_state(unit: str = "cryptobot") -> str:
    try:
        return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True,
                              timeout=10).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def journal_rejections(unit: str = "cryptobot", since: str = "-24h") -> list[str]:
    """Journal lines from the trading bot where Binance rejected the key (-2015)."""
    try:
        out = subprocess.run(["journalctl", "-u", unit, "--since", since, "--no-pager", "-o", "short-iso",
                              "--grep", "-2015"], capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return []
    return [line for line in out.splitlines() if "-2015" in line]


class Butler:
    def __init__(self, cfg: dict, tg, exchange, ai=None, repo_dir: str = ".", audit_path: str | None = None,
                 state_path: str | None = None, service_state=systemctl_state, rejections=journal_rejections,
                 public_ip=None, run=subprocess.run, clock=time.time, inbox_dir: str | None = None):
        from .runner import public_ip as default_ip

        self.cfg, self.tg, self.exchange, self.ai = cfg, tg, exchange, ai
        self.repo_dir = repo_dir
        self.audit_path = audit_path
        self.state_path = state_path
        self.service_state, self.rejections = service_state, rejections
        self.public_ip = public_ip or default_ip
        self.run_cmd, self.clock = run, clock
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        from .webpage import fetch_text
        self.fetch_page = fetch_text
        self.ex_lock = threading.Lock()  # ccxt clients are not thread-safe
        self.offset = self._load_offset()
        self.jobs: queue.Queue = queue.Queue()
        self.busy = threading.Event()
        self._stop = False
        self.pending: dict[str, dict] = {}  # confirmation token -> operation waiting for ✅
        self.pause_path = Path(cfg["state_dir"]) / f"{tag_for(cfg, 'live')}_paused.json"

    # -- persistence / audit ----------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _load_offset(self) -> int | None:
        offset = self._load_state().get("offset")
        return int(offset) if offset is not None else None

    def _save_state(self, **extra) -> None:
        if not self.state_path:
            return
        data = {**self._load_state(), **extra, "offset": self.offset}
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.state_path)

    def _save_offset(self) -> None:
        self._save_state()

    def audit(self, kind: str, detail: str = "") -> None:
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {kind} {detail}".rstrip()
        log.info("audit: %s", line)
        if self.audit_path:
            Path(self.audit_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(line.replace("\n", " ⏎ ") + "\n")

    # -- updates ------------------------------------------------------------------
    def poll(self) -> None:
        updates = self.tg.updates(self.offset)
        stale = 0
        for u in updates:
            self.offset = max(self.offset or 0, int(u.get("update_id", 0)) + 1)
            if u.get("callback_query"):
                try:
                    self.on_button(u["callback_query"])
                except Exception as exc:
                    log.exception("button failed")
                    self.tg.send(f"⚠️ 處理失敗：{str(exc)[:300]}")
                continue
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id"))
            if chat != self.tg.chat_id:
                self.audit("ignored", f"chat={chat} from={(msg.get('from') or {}).get('id')}")
                continue
            if self.clock() - float(msg.get("date") or 0) > STALE_SECONDS:
                stale += 1
                self.audit("stale", (msg.get("text") or msg.get("caption") or "[photo]")[:100])
                continue
            try:
                self.handle(msg)
            except Exception as exc:
                log.exception("message failed")
                self.tg.send(f"⚠️ 處理失敗：{str(exc)[:300]}")
        if updates:
            self._save_offset()
        self.expire()
        if stale:
            self.tg.send(f"⏭ 略過 {stale} 則管家離線時傳的舊訊息（超過 {STALE_SECONDS // 60} 分鐘），需要的話請重傳")

    def handle(self, msg: dict) -> None:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        text = BUTTONS.get(text, text)
        photo = self._photo_id(msg)
        if text.startswith("/") and not photo:
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            self.audit("command", text[:200])
            handler = {"/help": self.cmd_help, "/start": self.cmd_help, "/status": self.cmd_status,
                       "/ip": self.cmd_ip, "/scan": self.cmd_scan, "/new": self.cmd_new, "/pnl": self.cmd_pnl,
                       "/stop": self.cmd_stop, "/tp": self.cmd_tp, "/close": self.cmd_close,
                       "/pause": self.cmd_pause, "/resume": self.cmd_resume,
                       "/restart": self.cmd_restart, "/money": self.cmd_money, "/錢": self.cmd_money}.get(cmd)
            if handler is None:
                self.tg.send(f"不認得 {cmd}，輸入 /help 看可用指令")
                return
            from .manage import OpError
            try:
                handler(arg.strip())
            except OpError as exc:
                self.tg.send(f"❌ {exc}")
            return
        if text in ("狀態", "status"):
            self.audit("command", text)
            self.cmd_status("")
            return
        self.audit("ai", (text[:200] or "") + (" [photo]" if photo else ""))
        if self.ai is None:
            self.tg.send("AI 聊天尚未設定（缺 Claude Code 憑證），目前只能用指令，輸入 /help 查看")
            return
        if self.busy.is_set() or not self.jobs.empty():
            self.tg.send("⏳ 上一個問題還在分析，這則排在後面")
        self.jobs.put((text, photo))

    @staticmethod
    def _photo_id(msg: dict) -> str | None:
        if msg.get("photo"):
            return max(msg["photo"], key=lambda p: p.get("file_size") or p.get("width", 0))["file_id"]
        doc = msg.get("document") or {}
        if str(doc.get("mime_type", "")).startswith("image/"):
            return doc["file_id"]
        return None

    # -- commands -----------------------------------------------------------------
    def cmd_help(self, _arg: str) -> None:
        self.tg.send(HELP, keyboard=True)

    def snapshot(self) -> dict:
        with self.ex_lock:
            return account_snapshot(self.exchange, self.cfg)

    def cmd_status(self, _arg: str) -> None:
        try:
            snap = self.snapshot()
        except Exception as exc:
            from .runner import is_auth_error
            hint = "（幣安拒絕 API，多半是 IP 變了，輸入 /ip 查看）" if is_auth_error(exc) else ""
            self.tg.send(f"⚠️ 讀不到帳戶{hint}：{str(exc)[:200]}")
            return
        tz = self.cfg.get("notify", {}).get("timezone", "Asia/Taipei")
        text = format_status(snap, self.service_state("cryptobot"), tz) + self.money_text(snap)
        try:
            text += "\n\n" + self.ai_text()
        except Exception as exc:
            log.warning("ai section in status: %s", exc)
        self.tg.send(text)

    def maybe_daily_report(self) -> bool:
        """Daily account report at notify.daily_report (e.g. 09:00) while the trading bot is stopped
        (a running bot sends its own). Once per local day, remembered across restarts."""
        n = self.cfg.get("notify") or {}
        at = n.get("daily_report") or ""
        now = self.clock()
        if not at or now < getattr(self, "_report_retry", 0):
            return False
        from zoneinfo import ZoneInfo
        tz = n.get("timezone", "Asia/Taipei")
        local = datetime.fromtimestamp(now, ZoneInfo(tz))
        h, m = (int(x) for x in str(at).split(":"))
        day = local.strftime("%Y-%m-%d")
        if (local.hour, local.minute) < (h, m) or self._load_state().get("report_day") == day:
            return False
        state = self.service_state("cryptobot")
        if state == "active":
            return False
        try:
            snap = self.snapshot()
        except Exception as exc:
            log.warning("daily report: %s", str(exc)[:200])
            self._report_retry = now + 600
            return False
        text = format_status(snap, state, tz).replace("📊 即時狀態", "📊 每日報告", 1) + self.money_text(snap)
        try:
            text += "\n\n" + self.holdings_text(snap)
        except Exception as exc:
            log.warning("holdings in daily report: %s", exc)
        self.tg.send(text)
        self._save_state(report_day=day)
        self.audit("daily_report", day)
        return True

    def money_text(self, snap: dict) -> str:
        """Bot / manual / prediction split of the account result (empty when it can't be worked out)."""
        base = float((self.cfg.get("goals") or {}).get("base_capital") or 0)
        if base <= 0 or "equity" not in snap:
            return ""
        try:
            from datetime import datetime, timezone

            from .money import money_lines
            from .report import recent_trades
            bot_real = sum(float(r["pnl"]) for r in recent_trades(self.cfg, "live", datetime(1970, 1, 1, tzinfo=timezone.utc)))
            bot_unreal = sum(p["unrealized"] for p in snap.get("positions", []) if p.get("owner") == "bot")
            with self.ex_lock:
                lines = money_lines(self.exchange, self.cfg, snap["equity"], bot_real + bot_unreal)
            head = f"\n\n💼 損益（本金 {base:.2f} U，帳戶 {snap['equity'] - base:+.2f} U）"
            return head + f"\n  🤖 機器人：{bot_real + bot_unreal:+.2f} U\n" + "\n".join(lines)
        except Exception as exc:
            log.warning("money split: %s", exc)
            return ""

    def cmd_money(self, _arg: str) -> None:
        try:
            snap = self.snapshot()
        except Exception as exc:
            self.tg.send(f"⚠️ 讀不到主帳戶：{str(exc)[:200]}")
            return
        self.tg.send(self.holdings_text(snap))

    def holdings_text(self, snap: dict) -> str:
        from zoneinfo import ZoneInfo

        from .money import external_flows, flows_text, holdings, holdings_text, prediction_pnl, total_changes
        ai = self.ai_equity()
        with self.ex_lock:
            rows = holdings(self.exchange, snap["equity"], ai, prediction_pnl(self.exchange))
        tz = (self.cfg.get("notify") or {}).get("timezone", "Asia/Taipei")
        day = datetime.fromtimestamp(self.clock(), ZoneInfo(tz)).strftime("%Y-%m-%d")
        total = sum(v for _, v in rows if v)
        today, week = total_changes(self.cfg, day, total)
        text = holdings_text(rows, today, week)
        if all(v is not None for _, v in rows):  # a wallet missing from the total would look like a loss
            try:
                with self.ex_lock:
                    text += "\n" + flows_text(external_flows(self.exchange, self.cfg, total), tz)
            except Exception as exc:
                log.warning("flows: %s", exc)
        return text

    def ai_equity(self) -> float | None:
        """The AI trader's sub-account equity (its own API key, read-only here)."""
        try:
            env = dict(line.strip().split("=", 1) for line in open(AITRADER_ENV) if "=" in line)
            from .data import make_exchange
            ex = make_exchange(self.cfg, env["AITRADER_API_KEY"], env["AITRADER_API_SECRET"])
            return float(ex.fetch_balance()["info"]["totalMarginBalance"])
        except Exception as exc:
            log.warning("ai sub-account balance: %s", exc)
            return None

    def ai_text(self) -> str:
        """The AI trader's sub-account view (its own API key, read-only here)."""
        from .aitrader import status_text
        equity = self.ai_equity()

        def price(coin):
            with self.ex_lock:
                return self.exchange.fetch_ticker(f"{coin}/USDT:USDT")["last"]
        return status_text(self.cfg, price, equity)

    def cmd_ip(self, _arg: str) -> None:
        from .runner import is_auth_error

        api, seen = "✅ 幣安 API 連線正常", None
        try:
            with self.ex_lock:
                self.exchange.fetch_balance()
        except Exception as exc:
            if is_auth_error(exc):
                seen = self.public_ip(exc)
                api = f"⛔ 幣安拒絕 API（-2015），幣安看到的 IP：{seen}"
            else:
                api = f"⚠️ 連不上幣安：{str(exc)[:150]}"
        ip = seen or self.public_ip(None)
        lines = [f"🌐 VM 對外 IP：{ip}", api]
        rej = self.rejections()
        if rej:
            lines.append(f"🔑 最近 24 小時交易機器人被拒 {len(rej)} 次，最後一次：{rej[-1].split(' ')[0]}")
        else:
            lines.append("🔑 最近 24 小時交易機器人沒有被拒絕（-2015）")
        if seen:
            lines.append("請到幣安 API 管理 → 編輯 → IP 存取限制，加入上面的 IP")
        self.tg.send("\n".join(lines))

    def cmd_scan(self, _arg: str) -> None:
        self.tg.send("🔍 回撤掃描中，約 1–2 分鐘，結果會直接傳到這裡…")
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        try:
            r = self.run_cmd([sys.executable, "-m", "cryptobot", *SCAN_ARGS], cwd=self.repo_dir,
                             capture_output=True, text=True, timeout=900)
            if r.returncode == 0:
                self.tg.send("✅ 回撤掃描完成")
            else:
                self.tg.send(f"⚠️ 回撤掃描失敗：{(r.stderr or r.stdout or '')[-500:]}")
        except Exception as exc:
            self.tg.send(f"⚠️ 回撤掃描失敗：{str(exc)[:300]}")
        self.audit("scan-done")

    def cmd_pnl(self, arg: str) -> None:
        try:
            days = int(arg or 30)
        except ValueError:
            days = 0
        if not 1 <= days <= 90:
            self.tg.send("用法：/pnl 或 /pnl 天數（1–90），例如 /pnl 7")
            return
        self.tg.send(f"🧮 計算最近 {days} 天的損益（讀幣安成交紀錄，約 10 秒）…")
        threading.Thread(target=self._pnl, args=(days,), daemon=True).start()

    def _pnl(self, days: int) -> None:
        from .pnl import collect, format_pnl

        try:
            with self.ex_lock:
                data = collect(self.exchange, self.cfg, days)
            self.tg.send(format_pnl(data))
        except Exception as exc:
            log.exception("pnl failed")
            self.tg.send(f"⚠️ 損益計算失敗：{str(exc)[:300]}")

    def cmd_new(self, _arg: str) -> None:
        if self.ai is not None:
            self.ai.reset()
        self.tg.send("🆕 已開新的 AI 對話")

    # -- money operations (phase 2): propose, confirm with a button, execute --------------
    def propose(self, summary: str, action) -> None:
        """Show what will happen with ✅ / ❌; ``action()`` runs only after ✅ within the time limit."""
        import secrets

        token = secrets.token_hex(4)
        text = f"{summary}\n\n⏳ {CONFIRM_SECONDS} 秒內按 ✅ 確認才會執行"
        mid = self.tg.ask(text, token)
        if mid is None:
            self.tg.send("⚠️ 確認訊息送不出去，這個操作沒有執行")
            return
        self.pending[token] = {"action": action, "expires": self.clock() + CONFIRM_SECONDS,
                               "message_id": mid, "summary": summary}
        self.audit("propose", f"{token} {summary.splitlines()[0]} | {summary.splitlines()[1] if len(summary.splitlines()) > 1 else ''}")

    def offer(self, summary: str, options: list[tuple[str, object]]) -> None:
        """One button per option; picking one runs its callable (which proposes the real
        operation with ✅ / ❌), so a pick alone never places an order."""
        import secrets

        group = secrets.token_hex(4)
        tokens = [secrets.token_hex(4) for _ in options]
        mid = self.tg.choose(f"{summary}\n\n⏳ {CHOICE_SECONDS} 秒內有效",
                             [(label, f"pick:{t}") for (label, _), t in zip(options, tokens)])
        if mid is None:
            self.tg.send("⚠️ 選項訊息送不出去")
            return
        for (label, action), t in zip(options, tokens):
            self.pending[t] = {"action": action, "expires": self.clock() + CHOICE_SECONDS, "message_id": mid,
                               "summary": summary, "group": group, "label": label}

    def _drop_group(self, op: dict) -> None:
        for t, other in list(self.pending.items()):
            if op.get("group") and other.get("group") == op["group"]:
                del self.pending[t]

    def expire(self) -> None:
        for token, op in list(self.pending.items()):
            if token in self.pending and self.clock() > op["expires"]:
                del self.pending[token]
                self._drop_group(op)
                limit = CHOICE_SECONDS if op.get("group") else CONFIRM_SECONDS
                self.tg.close_prompt(op["message_id"], f"{op['summary']}\n\n⌛ 超過 {limit} 秒沒"
                                                       f"{'選' if op.get('group') else '確認'}，已作廢")
                self.audit("expired", token)

    def on_button(self, cq: dict) -> None:
        chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
        if chat != self.tg.chat_id or str((cq.get("from") or {}).get("id")) != self.tg.chat_id:
            self.audit("ignored-button", f"chat={chat} from={(cq.get('from') or {}).get('id')}")
            self.tg.answer_button(cq["id"])
            return
        verdict, _, token = str(cq.get("data", "")).partition(":")
        op = self.pending.pop(token, None)
        if op is None:
            self.tg.answer_button(cq["id"], "這個確認已經失效")
            return
        if self.clock() > op["expires"]:
            self._drop_group(op)
            self.tg.answer_button(cq["id"], "已逾時作廢")
            self.tg.close_prompt(op["message_id"], f"{op['summary']}\n\n⌛ 已逾時作廢")
            self.audit("expired", token)
            return
        if op.get("group"):  # a suggested level was picked: show the real confirmation next
            self._drop_group(op)
            if verdict != "pick":
                self.tg.answer_button(cq["id"])
                return
            self.tg.answer_button(cq["id"], f"已選 {op['label']}")
            self.tg.close_prompt(op["message_id"], f"{op['summary']}\n\n👉 已選 {op['label']}")
            self.audit("picked", f"{token} {op['label']}")
            from .manage import OpError
            try:
                op["action"]()
            except OpError as exc:
                self.tg.send(f"❌ {exc}")
            return
        if verdict != "ok":
            self.tg.answer_button(cq["id"], "已取消")
            self.tg.close_prompt(op["message_id"], f"{op['summary']}\n\n❌ 已取消，沒有下單")
            self.audit("cancelled", token)
            return
        self.tg.answer_button(cq["id"], "執行中…")
        self.tg.close_prompt(op["message_id"], f"{op['summary']}\n\n⏳ 已確認，執行中…")
        self.audit("confirmed", token)
        from .manage import OpError
        try:
            result = op["action"]()
        except OpError as exc:
            result = f"❌ 沒有執行：{exc}"
        except Exception as exc:
            log.exception("operation failed")
            result = f"⚠️ 執行失敗：{str(exc)[:400]}"
        self.audit("result", f"{token} {result}")
        self.tg.close_prompt(op["message_id"], f"{op['summary']}\n\n✅ 已確認（結果見下一則）")
        self.tg.send(result)

    def _trade(self, plan) -> str:
        from .manage import execute

        with self.ex_lock:
            return execute(self.exchange, plan)

    def cmd_stop(self, arg: str) -> None:
        from .manage import parse_coin, parse_price, plan_stop

        parts = arg.split()
        if not parts:
            self.pick_position("stop")
            return
        if len(parts) == 1:
            self.suggest_levels(parse_coin(parts[0]), "stop")
            return
        if len(parts) != 2:
            self.tg.send("用法：/stop 幣種 價格，例如 /stop BTC 84000；只打 /stop BTC 會列出建議點位")
            return
        plan = plan_stop(self.snapshot(), parse_coin(parts[0]), parse_price(parts[1]))
        self.propose(plan.summary, lambda: self._trade(plan))

    def cmd_tp(self, arg: str) -> None:
        from .manage import parse_coin, parse_price, plan_tp

        parts = arg.split()
        if not parts:
            self.pick_position("tp")
            return
        if len(parts) == 1:
            self.suggest_levels(parse_coin(parts[0]), "tp")
            return
        if len(parts) not in (2, 3):
            self.tg.send("用法：/tp 幣種 價格 [數量]，例如 /tp BTC 80000 50%（不寫數量會讓你選 25～100%）；"
                         "只打 /tp BTC 會列出建議點位")
            return
        coin, price = parse_coin(parts[0]), parse_price(parts[1])
        snap = self.snapshot()
        if len(parts) == 2:  # no amount: pick how much of the position to take off
            plan_tp(snap, coin, price, "100%")  # validates side / price before asking
            from .manage import find_position
            held = find_position(snap, coin)["amount"]
            self.offer(f"🎯 {coin} 止盈 {price:.10g}：要平掉多少？（持倉 {held:.6g}）",
                       [(f"{pct}%（約 {held * pct / 100:.6g}）", lambda pct=pct: self.cmd_tp(f"{coin} {price:.10g} {pct}%"))
                        for pct in (25, 50, 75, 100)])
            return
        plan = plan_tp(snap, coin, price, parts[2])
        self.propose(plan.summary, lambda: self._trade(plan))

    def pick_position(self, kind: str) -> None:
        """Tap-only flow: every open position as a button, then levels / amounts, then ✅."""
        snap = self.snapshot()
        if not snap["positions"]:
            self.tg.send("目前沒有持倉")
            return
        title = {"stop": "🛑 要掛／移動哪一個的停損？", "tp": "🎯 要掛哪一個的止盈？", "close": "💥 要平哪一個？"}[kind]

        def label(p):
            side = "多" if p["side"] == "long" else "空"
            return f"{'🤖' if p['owner'] == 'bot' else '✋'} {p['coin']} {side}　{p['unrealized']:+.2f} U"

        def action(p):
            if kind in ("stop", "tp"):
                return lambda: self.suggest_levels(p["coin"], kind)
            return lambda: self.offer(
                f"💥 {p['coin']} 要平掉多少？（持倉 {p['amount']:.6g}，未實現 {p['unrealized']:+.2f} U）",
                [(f"{pct}%（約 {p['unrealized'] * pct / 100:+.2f} U）", lambda pct=pct: self.cmd_close(f"{p['coin']} {pct}%"))
                 for pct in (25, 50, 75, 100)])
        self.offer(title + "\n（要自訂價格可直接打，例如 /stop BTC 84000）",
                   [(label(p), action(p)) for p in snap["positions"]])

    def suggest_levels(self, coin: str, kind: str) -> None:
        from .manage import find_position, suggest

        snap = self.snapshot()
        sym = find_position(snap, coin)["symbol"]
        with self.ex_lock:
            self.exchange.load_markets()
            h4 = self.exchange.fetch_ohlcv(sym, "4h", limit=60)
            d1 = self.exchange.fetch_ohlcv(sym, "1d", limit=30)
            text, prices = suggest(snap, coin, kind, h4, d1,
                                   rnd=lambda x: float(self.exchange.price_to_precision(sym, x)))
        cmd = self.cmd_stop if kind == "stop" else self.cmd_tp
        if not prices:
            self.tg.send(text)
            return
        self.offer(text, [(f"{i}. {'停損' if kind == 'stop' else '止盈'} {x:.10g}",
                           lambda x=x: cmd(f"{coin} {x:.10g}")) for i, x in enumerate(prices, 1)])

    def cmd_close(self, arg: str) -> None:
        from .manage import parse_coin, plan_close

        parts = arg.split()
        if not parts:
            self.pick_position("close")
            return
        if len(parts) not in (1, 2):
            self.tg.send("用法：/close 幣種 [百分比]，例如 /close BTC 或 /close BTC 50%")
            return
        plan = plan_close(self.snapshot(), parse_coin(parts[0]), parts[1] if len(parts) == 2 else "100%")
        self.propose(plan.summary, lambda: self._trade(plan))

    def cmd_pause(self, _arg: str) -> None:
        """Stopping new entries only lowers risk, so it needs no confirmation."""
        if self.pause_path.exists():
            self.tg.send("⏸ 交易機器人本來就是暫停開新倉狀態，/resume 可恢復")
            return
        self.pause_path.parent.mkdir(parents=True, exist_ok=True)
        self.pause_path.write_text(json.dumps({"paused_at": datetime.now(timezone.utc).isoformat()}))
        self.audit("paused")
        state = self.service_state("cryptobot")
        note = ("交易機器人幾秒內會回覆「⏸ 機器人已暫停開新倉」" if state == "active"
                else f"（交易機器人目前是 {state}，啟動後就會照暫停狀態運作）")
        self.tg.send(f"⏸ 已設定暫停開新倉：現有持倉照常管理停損與出場。\n{note}")

    def cmd_resume(self, _arg: str) -> None:
        if not self.pause_path.exists():
            self.tg.send("▶️ 交易機器人沒有暫停，正在正常開新倉")
            return

        def resume():
            self.pause_path.unlink(missing_ok=True)
            return "▶️ 已解除暫停：交易機器人下一次掃描起會照訊號開新倉"
        self.propose("▶️ 恢復交易機器人開新倉\n之後每 4 小時掃描到訊號就會用真實資金開倉", resume)

    def cmd_restart(self, _arg: str) -> None:
        def restart():
            r = self.run_cmd(["systemctl", "restart", "cryptobot"], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return f"⚠️ 重啟失敗：{(r.stderr or r.stdout or '')[-300:]}"
            time.sleep(5)
            state = self.service_state("cryptobot")
            return (f"🔄 交易機器人已重啟，狀態：{state}" if state == "active"
                    else f"⚠️ 已送出重啟，但目前狀態是 {state}，請用 /status 或看日誌確認")
        self.propose(f"🔄 重啟交易機器人（systemctl restart cryptobot，目前 {self.service_state('cryptobot')}）\n"
                     "重啟期間大約 1 分鐘不會掃描，現有持倉的交易所停損單照常有效", restart)

    # -- AI worker ------------------------------------------------------------------
    def answer(self, text: str, photo: str | None) -> str:
        image = None
        if photo:
            if self.inbox_dir is None:
                return "⚠️ 沒有設定圖片暫存目錄"
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            image = self.tg.download(photo, self.inbox_dir / f"{int(self.clock() * 1000)}.jpg")
            image.chmod(0o644)
        try:
            snap = self.snapshot()
        except Exception as exc:
            snap = {"error": f"讀不到帳戶：{str(exc)[:200]}"}
        from .webpage import attach_pages  # the AI has no internet: links are fetched here
        return self.ai.ask(attach_pages(text, self.fetch_page), image=image, snapshot=snap)

    def worker(self) -> None:
        while not self._stop:
            try:
                text, photo = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            self.busy.set()
            done = threading.Event()
            threading.Thread(target=self._keep_typing, args=(done,), daemon=True).start()
            try:
                reply = self.answer(text, photo)
            except Exception as exc:
                log.exception("ai failed")
                reply = f"⚠️ AI 回覆失敗：{str(exc)[:300]}"
            finally:
                done.set()
                self.busy.clear()
            self.tg.send(reply)

    def _keep_typing(self, done: threading.Event) -> None:
        while not done.is_set():
            self.tg.typing()
            done.wait(4)

    # -- main loop ------------------------------------------------------------------
    def stop(self, *_):
        self._stop = True

    def run(self) -> None:
        import signal

        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        if self.offset is None:  # first start: skip whatever is already waiting
            backlog = self.tg.updates(-1, timeout=0)
            self.offset = int(backlog[-1]["update_id"]) + 1 if backlog else None
        threading.Thread(target=self.worker, daemon=True).start()
        self.tg.setup()
        if self._load_state().get("keyboard") != KEYBOARD_VERSION:  # replace the old reply keyboard once
            self.tg.send("🤵 管家已更新：可以管理部位與控制交易機器人了，輸入 /help 查看", keyboard=True)
            self._save_state(keyboard=KEYBOARD_VERSION)
        self.audit("start")
        errors = 0
        while not self._stop:
            try:
                self.poll()
                errors = 0
                self.maybe_daily_report()
            except Exception as exc:
                errors += 1  # a single long-poll timeout is normal on a slow line
                log.log(logging.INFO if errors == 1 else logging.WARNING, "poll failed (%d): %s", errors, str(exc)[:200])
                time.sleep(min(60, 2 ** min(errors, 6)))
        self.audit("stop")


def main(cfg: dict, repo_dir: str = ".") -> int:
    from .ai import ClaudeChat
    from .data import make_exchange

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise SystemExit("butler needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (see .env.example)")
    key, secret = os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_API_SECRET")
    if not key or not secret:
        raise SystemExit("butler needs EXCHANGE_API_KEY / EXCHANGE_API_SECRET to read the account")
    exchange = make_exchange(cfg, key, secret, os.getenv("EXCHANGE_API_PASSWORD"))
    ai = ClaudeChat.from_env(cfg, repo_dir)
    if ai is None:
        log.warning("AI chat disabled: CLAUDE_CODE_OAUTH_TOKEN or the claude binary is missing")
    b = Butler(cfg, TelegramBot(token, chat), exchange, ai=ai, repo_dir=repo_dir,
               audit_path=str(Path(cfg["log_dir"]) / "butler_audit.log"),
               state_path=str(Path(cfg["state_dir"]) / "butler.json"),
               inbox_dir=ai.inbox_dir if ai else None)
    b.run()
    return 0
