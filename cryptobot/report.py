"""Status report: account balance, open positions, recent trades.

Sent to Telegram once a day by the scanner (``notify.daily_report``) and
printed on demand with ``python3 -m cryptobot status``.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .scanner import tag_for


def _local(now: datetime, tz: str) -> pd.Timestamp:
    ts = pd.Timestamp(now)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return ts.tz_convert(tz)


def recent_trades(cfg: dict, mode: str, since: datetime) -> list[dict]:
    path = Path(cfg["log_dir"]) / f"{tag_for(cfg, mode)}_trades.csv"
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                if pd.Timestamp(row["closed_at"]).tz_convert("UTC") >= pd.Timestamp(since):
                    out.append(row)
            except Exception:
                continue
    return out


def build_report(scanner, exchange, cfg: dict, mode: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    tz = cfg.get("notify", {}).get("timezone", "Asia/Taipei")
    lines = [f"📊 每日報告 {_local(now, tz):%m/%d %H:%M}（{'實盤' if mode == 'live' else '模擬'}）"]

    open_syms = scanner.open_symbols()
    prices = {}
    if open_syms:
        try:
            tickers = exchange.fetch_tickers(open_syms)
            prices = {s: float((tickers.get(s) or {}).get("last") or 0) for s in open_syms}
        except Exception:
            prices = {}
        for sym, px in prices.items():  # paper accounts value positions at the latest price
            broker = scanner.traders[sym].broker
            if px and not broker.live:
                broker.mark = px

    equity = scanner.account_equity()
    base = float((cfg.get("goals") or {}).get("base_capital") or 0)
    if equity is not None:
        line = f"💰 餘額 {equity:.2f} U"
        if base > 0:
            line += f"（本金 {base:.0f} U，{(equity / base - 1) * 100:+.1f}%）"
        lines.append(line)
        peak = scanner.risk.peak_equity
        if peak:
            lines.append(f"📉 距離最高點 {(equity / peak - 1) * 100:+.1f}%（停機線 -{cfg['risk']['max_drawdown'] * 100:.0f}%）")

    if open_syms:
        lines.append(f"📌 持倉 {len(open_syms)}/{scanner.max_positions}：")
        for sym in open_syms:
            t = scanner.traders[sym]
            pos = t.position
            name = sym.split("/")[0]
            if pos is None:
                lines.append(f"  • {name} 限價單等待成交")
                continue
            px = prices.get(sym) or 0
            s = 1 if pos.side == "long" else -1
            pnl = s * (px - pos.entry_price) * pos.amount if px else 0.0
            lines.append(f"  • {name} {'多' if s > 0 else '空'} @ {pos.entry_price:.6g} → {px:.6g}"
                         f"  {pnl:+.2f} U  停損 {pos.stop_price:.6g}")
    else:
        lines.append("📌 目前沒有持倉，等待突破訊號")

    trades = recent_trades(cfg, mode, now - timedelta(hours=24))
    if trades:
        total = sum(float(r["pnl"]) for r in trades)
        wins = sum(1 for r in trades if float(r["pnl"]) > 0)
        lines.append(f"🧾 過去 24 小時平倉 {len(trades)} 筆（賺 {wins} 筆）合計 {total:+.2f} U")
    else:
        lines.append("🧾 過去 24 小時沒有平倉")

    if scanner.risk.drawdown_halt:
        lines.append("⛔ 已觸發最大回撤停機，不會開新倉")
    elif scanner.target_reached:
        lines.append("🏁 已達目標，不再開新倉")
    lines.append("✅ 機器人運作中")
    return "\n".join(lines)


class DailyReporter:
    """Sends the report once per local day after ``notify.daily_report`` (e.g. "09:00")."""

    def __init__(self, cfg: dict):
        n = cfg.get("notify", {})
        self.at = n.get("daily_report") or ""
        self.tz = n.get("timezone", "Asia/Taipei")
        self.last_day: str | None = None

    def due(self, now: datetime) -> bool:
        if not self.at:
            return False
        local = _local(now, self.tz)
        h, m = (int(x) for x in str(self.at).split(":"))
        day = local.strftime("%Y-%m-%d")
        if day == self.last_day or (local.hour, local.minute) < (h, m):
            return False
        self.last_day = day
        return True

