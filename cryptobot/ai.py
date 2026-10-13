"""AI chat for the Telegram butler: Claude Code in non-interactive mode, strictly read-only.

Read-only is enforced in layers, not by the prompt:
  * Claude Code runs as the unprivileged user ``butlerai``, who cannot read /root (the repo,
    .env, API keys) and therefore cannot reach the exchange account at all.
  * It only sees a workspace the butler (root) fills before each question: an account snapshot,
    config.yaml and the trade journal (work/). The files are root-owned and not writable by butlerai.
  * Tools are limited to Read and Bash (--tools); permissions (deploy/ai/settings.json) only
    allow the public market-data script and reads inside the workspace, everything else is
    denied without asking (--permission-mode dontAsk), and Bash runs in the OS sandbox.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

AI_ROOT = "/var/lib/cryptobot-ai"  # home/ (butlerai), work/ and inbox/ (root-owned, readable)
TOOLS_DIR = "/opt/cryptobot-ai"  # settings.json, system-prompt.md, market.py (root-owned)
AI_USER = "butlerai"
SHIPPED = ("settings.json", "system-prompt.md", "market.py")  # copied from deploy/ai/ on start


class ClaudeChat:
    def __init__(self, cfg: dict, repo_dir: str, claude_bin: str, token: str, root: str = AI_ROOT,
                 tools_dir: str = TOOLS_DIR, user: str | None = AI_USER, run=subprocess.run, clock=time.time):
        b = cfg.get("butler") or {}
        self.cfg, self.repo_dir = cfg, Path(repo_dir)
        self.claude_bin, self.token, self.user = claude_bin, token, user
        self.root, self.tools_dir = Path(root), Path(tools_dir)
        self.home, self.work_dir, self.inbox_dir = self.root / "home", self.root / "work", self.root / "inbox"
        # Claude Code runs in an empty directory owned by butlerai: the Bash sandbox needs to create
        # read-only placeholders (.claude/settings.json, .mcp.json ...) in the working directory.
        self.cwd = self.root / "cwd"
        self.model = b.get("model", "sonnet")
        self.idle = float(b.get("idle_hours", 2)) * 3600
        self.max_turns = int(b.get("max_turns", 10))
        self.run, self.clock = run, clock
        self.session_id: str | None = None
        self.last_at = 0.0

    @classmethod
    def from_env(cls, cfg: dict, repo_dir: str) -> "ClaudeChat | None":
        token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        claude = Path(AI_ROOT) / "home" / ".local" / "bin" / "claude"
        if not token or not claude.exists():
            return None
        chat = cls(cfg, repo_dir, str(claude), token)
        chat.install()
        return chat

    def install(self) -> None:
        """Refresh the root-owned settings, prompt and market script from the repo (deploy/ai/)."""
        self.tools_dir.mkdir(parents=True, exist_ok=True)
        for name in SHIPPED:
            dest = self.tools_dir / name
            shutil.copyfile(self.repo_dir / "deploy" / "ai" / name, dest)
            dest.chmod(0o644)
        self.tools_dir.chmod(0o755)

    def reset(self) -> None:
        self.session_id = None

    # -- workspace ------------------------------------------------------------
    def prepare(self, snapshot: dict) -> None:
        """What the AI may read: account snapshot, bot config and trade journal (copies)."""
        logs = self.work_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "account.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
        cfg_file = self.repo_dir / "config.yaml"
        if cfg_file.exists():
            shutil.copyfile(cfg_file, self.work_dir / "config.yaml")
        for f in (self.repo_dir / self.cfg.get("log_dir", "logs")).glob("*_trades.csv"):
            shutil.copyfile(f, logs / f.name)
        portfolio = self.repo_dir / self.cfg.get("state_dir", "state") / "binance_scan_live_portfolio.json"
        if portfolio.exists():
            shutil.copyfile(portfolio, self.work_dir / "bot_risk.json")
        for p in [self.work_dir, logs, *self.work_dir.rglob("*")]:
            p.chmod(0o755 if p.is_dir() else 0o644)
        cutoff = self.clock() - 86400
        for img in self.inbox_dir.glob("*") if self.inbox_dir.exists() else []:
            if img.stat().st_mtime < cutoff:
                img.unlink(missing_ok=True)

    # -- one question -----------------------------------------------------------
    def command(self, prompt: str) -> list[str]:
        cmd = [self.claude_bin, "-p", prompt, "--output-format", "json", "--model", self.model,
               "--max-turns", str(self.max_turns), "--tools", "Read,Bash", "--permission-mode", "dontAsk",
               "--settings", str(self.tools_dir / "settings.json"), "--strict-mcp-config",
               "--append-system-prompt-file", str(self.tools_dir / "system-prompt.md")]
        if self.session_id:
            cmd += ["--resume", self.session_id]
        return cmd

    def _call(self, prompt: str) -> dict:
        env = {"HOME": str(self.home), "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
               "PATH": f"{self.home}/.local/bin:/usr/local/bin:/usr/bin:/bin", "SHELL": "/bin/bash",
               "LANG": "C.UTF-8", "CLAUDE_CODE_OAUTH_TOKEN": self.token, "DISABLE_AUTOUPDATER": "1"}
        kw = {"user": self.user} if self.user else {}
        r = self.run(self.command(prompt), cwd=str(self.cwd), env=env, capture_output=True,
                     text=True, timeout=600, **kw)
        for line in reversed((r.stdout or "").strip().splitlines()):
            try:
                return json.loads(line)
            except ValueError:
                continue
        raise RuntimeError(f"claude exited {r.returncode}: {(r.stderr or r.stdout or '')[-300:]}")

    def ask(self, text: str, image: Path | None = None, snapshot: dict | None = None) -> str:
        note = ""
        if self.session_id and self.clock() - self.last_at > self.idle:
            self.session_id = None
            note = f"🆕 超過 {self.idle / 3600:g} 小時沒對話，已開新對話\n\n"
        self.prepare(snapshot or {})
        prompt = text or "請分析這張圖"
        prompt += f"\n\n（帳戶快照已更新：{self.work_dir / 'account.json'}，{(snapshot or {}).get('time', '')}）"
        if image:
            prompt += f"\n（使用者傳來的圖片：{image}，請用 Read 讀取後分析）"
        resumed = self.session_id is not None
        data = self._call(prompt)
        if data.get("is_error") and resumed and "session" in str(data.get("result", "")).lower():
            self.session_id = None  # the saved conversation is gone: start a fresh one
            data = self._call(prompt)
        self.last_at = self.clock()
        if data.get("session_id"):
            self.session_id = data["session_id"]
        log.info("ai reply: turns=%s cost=%s usage=%s", data.get("num_turns"), data.get("total_cost_usd"),
                 {k: v for k, v in (data.get("usage") or {}).items() if "tokens" in k})
        result = str(data.get("result") or "").strip()
        if data.get("subtype") == "error_max_turns":
            return note + (result or "⚠️ 分析步驟太多被中斷，請把問題問得更具體一點")
        if data.get("is_error"):
            return note + f"⚠️ AI 回覆失敗：{result[:300] or data.get('subtype')}"
        return note + (result or "（AI 沒有回覆內容）")
