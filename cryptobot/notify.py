"""Telegram notifications (failures are logged, never raised)."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str, prefix: str = ""):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.prefix = prefix

    def send(self, text: str) -> bool:
        body = json.dumps({"chat_id": self.chat_id, "text": f"{self.prefix}{text}"}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            return False

    __call__ = send


def make_notifier(cfg: dict, mode: str, label: str | None = None):
    if not cfg.get("notify", {}).get("telegram"):
        return None
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return None
    tag = "" if mode == "live" else "[模擬] "
    return Telegram(token, chat, prefix=f"{tag}{label or cfg['symbol']} ")
