"""Telegram notifications (failures are logged, never raised)."""

from __future__ import annotations

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str, prefix: str = ""):
        self.api = f"https://api.telegram.org/bot{token}"
        self.url = f"{self.api}/sendMessage"
        self.chat_id = chat_id
        self.prefix = prefix
        self._offset: int | None = None  # None = skip messages sent before the bot started

    def _get_updates(self, offset: int | None) -> list[dict]:
        url = f"{self.api}/getUpdates?timeout=0" + (f"&offset={offset}" if offset is not None else "")
        with urllib.request.urlopen(url, timeout=8) as resp:
            return json.loads(resp.read().decode()).get("result", [])

    def poll_commands(self) -> list[str]:
        """New text messages from the configured chat (e.g. "/status"). Never raises."""
        try:
            updates = self._get_updates(self._offset if self._offset is not None else -1)
        except Exception as exc:
            log.debug("telegram poll failed: %s", exc)
            return []
        first_poll = self._offset is None
        texts = []
        for u in updates:
            self._offset = max(self._offset or 0, int(u.get("update_id", 0)) + 1)
            msg = u.get("message") or {}
            if first_poll or str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                continue  # old backlog, or someone else talking to the bot
            if msg.get("text"):
                texts.append(msg["text"].strip())
        if first_poll and self._offset is None:
            self._offset = 0
        return texts

    def send(self, text: str) -> bool:
        body = json.dumps({"chat_id": self.chat_id, "text": f"{self.prefix}{text}"}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception as exc:
            log.warning("telegram send failed: %s", exc)
            return False

    def __call__(self, text: str) -> bool:
        return self.send(text)


def make_notifier(cfg: dict, mode: str, label: str | None = None):
    if not cfg.get("notify", {}).get("telegram"):
        return None
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.warning("telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return None
    tag = "" if mode == "live" else "[模擬] "
    return Telegram(token, chat, prefix=f"{tag}{label or cfg['symbol']} ")
