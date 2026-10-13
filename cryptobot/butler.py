"""Telegram butler: the only process that reads the chat (getUpdates).

    python -m cryptobot butler          # systemd: deploy/cryptobot-telegram.service

Runs independently of the trading bot so it still answers when the bot is stopped or
crashed. Only messages from TELEGRAM_CHAT_ID are handled; every command is written to the
audit log. Phase 1 is read-only: /help /status /ip /scan /new, and plain text or photos go
to the AI (cryptobot/ai.py), which only ever sees a snapshot of the account.
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

HELP = """🤵 Telegram 管家指令
/status　帳戶總額、可用餘額、所有持倉（🤖 機器人／✋ 手動）、停損止盈、未實現損益
/ip　VM 對外 IP、幣安 API 是否接受連線、最近 24 小時有沒有 -2015 拒絕
/scan　立刻跑一次回撤掃描（和每天 09:00 的一樣），結果直接傳到這裡
/new　AI 開新對話（超過 2 小時沒講話也會自動開新的）
/help　顯示這份說明

直接打字或傳 K 線截圖 → AI 分析（唯讀：看得到行情、帳戶、持倉、掛單、設定與交易紀錄，不能下單或改任何東西）"""


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
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", params, timeout=timeout + 10)

    def send(self, text: str) -> bool:
        ok = True
        for part in split_message(text):
            try:
                self._call("sendMessage", {"chat_id": self.chat_id, "text": part})
            except Exception as exc:
                log.warning("telegram send failed: %s", exc)
                ok = False
        return ok

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
            "leverage": p.get("leverage"), "margin_mode": p.get("marginMode"), "orders": orders,
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
        lines.append(f"🤖 交易機器人：{'運作中' if bot_state == 'active' else f'⚠️ {bot_state}'}")
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
        lines.append(f"    停損 {_orders_line(p['orders'], 'stop')}｜止盈 {_orders_line(p['orders'], 'tp')}")
        if not any(o["kind"] == "stop" for o in p["orders"]):
            missing.append(p["coin"])
    if missing:
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
        self.ex_lock = threading.Lock()  # ccxt clients are not thread-safe
        self.offset = self._load_offset()
        self.jobs: queue.Queue = queue.Queue()
        self.busy = threading.Event()
        self._stop = False

    # -- persistence / audit ----------------------------------------------------
    def _load_offset(self) -> int | None:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                return int(json.load(fh)["offset"])
        except Exception:
            return None

    def _save_offset(self) -> None:
        if not self.state_path:
            return
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"offset": self.offset}, fh)
        os.replace(tmp, self.state_path)

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
        if stale:
            self.tg.send(f"⏭ 略過 {stale} 則管家離線時傳的舊訊息（超過 {STALE_SECONDS // 60} 分鐘），需要的話請重傳")

    def handle(self, msg: dict) -> None:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        photo = self._photo_id(msg)
        if text.startswith("/") and not photo:
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            self.audit("command", text[:200])
            handler = {"/help": self.cmd_help, "/start": self.cmd_help, "/status": self.cmd_status,
                       "/ip": self.cmd_ip, "/scan": self.cmd_scan, "/new": self.cmd_new}.get(cmd)
            if handler is None:
                self.tg.send(f"不認得 {cmd}，輸入 /help 看可用指令")
                return
            handler(arg.strip())
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
        self.tg.send(HELP)

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
        self.tg.send(format_status(snap, self.service_state("cryptobot"), tz))

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

    def cmd_new(self, _arg: str) -> None:
        if self.ai is not None:
            self.ai.reset()
        self.tg.send("🆕 已開新的 AI 對話")

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
        return self.ai.ask(text, image=image, snapshot=snap)

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
        self.audit("start")
        errors = 0
        while not self._stop:
            try:
                self.poll()
                errors = 0
            except Exception as exc:
                errors += 1
                log.warning("poll failed (%d): %s", errors, str(exc)[:200])
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
