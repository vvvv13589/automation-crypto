"""Realized PnL split between the trading bot and manual trading (butler /pnl).

Built from the exchange's own records, so transfers in/out never count as profit:
  * fills (userTrades: realized PnL and commission per fill) and funding payments (income),
  * the order behind each fill (allOrders: clientOrderId shows where it was placed).
Fills are grouped into round trips per coin (flat -> flat), walking back from the current
position so a trip that started before the period is still complete.
A trip is the bot's when its fills fall inside one of the bot's own trades (journal + open
positions in its state files) and were not placed from the Binance app; everything else
(app, web, other scripts, butler orders on manual positions) is manual.
"""

from __future__ import annotations

import csv
import glob
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .scanner import tag_for

DAY_MS = 86_400_000
CHUNK_MS = 7 * DAY_MS - 1  # userTrades / allOrders accept at most 7 days per request
APP_PREFIXES = ("ios_", "android_", "web_", "electron_", "and_")  # placed by hand in Binance
LOOKBACK_DAYS = 60  # history fetched to rebuild trips that started before the period


@dataclass
class Trip:
    symbol: str  # exchange id, e.g. BTCUSDT
    side: str  # long | short
    opened: int  # ms
    closed: int | None  # ms; None = still open
    pnl: float = 0.0  # realized PnL - commission + funding
    fills: list = field(default_factory=list)
    owner: str = "manual"

    @property
    def coin(self) -> str:
        return self.symbol[:-4] if self.symbol.endswith("USDT") else self.symbol


# ------------------------------------------------------------------ exchange data
def _chunks(since: int, until: int):
    t = since
    while t < until:
        yield t, min(t + CHUNK_MS, until)
        t += CHUNK_MS + 1


def fetch_income(ex, since: int, until: int) -> list[dict]:
    out, t = [], since
    while t < until:
        rows = ex.fapiPrivateGetIncome({"startTime": t, "endTime": until, "limit": 1000})
        out += rows
        if len(rows) < 1000:
            break
        t = int(rows[-1]["time"]) + 1
    return out


def fetch_fills(ex, symbol: str, since: int, until: int) -> list[dict]:
    """Fills with the clientOrderId of their order, oldest first."""
    fills, clients = [], {}
    for a, b in _chunks(since, until):
        got = ex.fapiPrivateGetUserTrades({"symbol": symbol, "startTime": a, "endTime": b, "limit": 1000})
        if not got:
            continue
        fills += got
        for o in ex.fapiPrivateGetAllOrders({"symbol": symbol, "startTime": a, "endTime": b, "limit": 1000}):
            clients[str(o["orderId"])] = o.get("clientOrderId") or ""
    out = [{"symbol": symbol, "time": int(f["time"]), "id": int(f["id"]), "side": f["side"],
            "qty": float(f["qty"]), "realized": float(f["realizedPnl"]),
            "commission": float(f["commission"]) if f.get("commissionAsset", "USDT") == "USDT" else 0.0,
            "client": clients.get(str(f["orderId"]), "")} for f in fills]
    return sorted(out, key=lambda f: (f["time"], f["id"]))


# ------------------------------------------------------------------ trips
def build_trips(fills: list[dict], position: float) -> list[Trip]:
    """Round trips for one symbol. ``position`` is the signed amount held now; walking back
    from it gives the position before every fill, so the first trip is never cut in half."""
    signed = [f["qty"] if f["side"] == "BUY" else -f["qty"] for f in fills]
    before, pos = [], position
    for q in reversed(signed):
        pos -= q
        before.append(pos)
    before.reverse()
    trips, cur = [], None
    for f, q, pb in zip(fills, signed, before):
        pa = pb + q
        if cur is None:
            if abs(pb) > 1e-12:  # already in a position when our history starts: skip its tail
                continue
            cur = Trip(f["symbol"], "long" if q > 0 else "short", f["time"], None)
        cur.fills.append(f)
        cur.pnl += f["realized"] - f["commission"]
        if abs(pa) < 1e-9 or pa * pb < 0:  # flat again (or flipped through zero)
            cur.closed = f["time"]
            trips.append(cur)
            cur = None
            if pa * pb < 0:  # a flip opens the next trip with the remainder
                cur = Trip(f["symbol"], "long" if pa > 0 else "short", f["time"], None, fills=[])
    if cur is not None:
        trips.append(cur)
    return trips


def add_funding(trips: list[Trip], income: list[dict]) -> None:
    for r in income:
        if r.get("incomeType") != "FUNDING_FEE":
            continue
        t, sym = int(r["time"]), r["symbol"]
        for tr in trips:
            if tr.symbol == sym and tr.opened <= t <= (tr.closed or t):
                tr.pnl += float(r["income"])
                break


# ------------------------------------------------------------------ bot or manual
def _ms(ts: str) -> int:
    t = pd.Timestamp(ts)
    return int((t if t.tzinfo else t.tz_localize("UTC")).timestamp() * 1000)


def bot_windows(cfg: dict, now_ms: int) -> list[tuple[str, int, int, float]]:
    """(exchange id, start ms, end ms, R) for every bot trade, closed (journal) or open (state)."""
    tag = tag_for(cfg, "live")
    out = []
    journal = Path(cfg["log_dir"]) / f"{tag}_trades.csv"
    if journal.exists():
        with open(journal, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:  # early journal rows have no symbol: they match any coin (still not app orders)
                    out.append((_raw(row["symbol"]) if row.get("symbol") else None, _ms(row["opened_at"]),
                                _ms(row["closed_at"]), float(row.get("r_multiple") or 0)))
                except Exception:
                    continue
    for f in glob.glob(str(Path(cfg["state_dir"]) / f"{tag}_*.json")):
        try:
            st = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        pos = st.get("position") or {}
        if st.get("symbol") and pos.get("opened_at"):
            out.append((_raw(st["symbol"]), _ms(pos["opened_at"]), now_ms, float("nan")))
    return out


def _raw(symbol: str) -> str:
    return symbol.split(":")[0].replace("/", "")


def classify(trip: Trip, windows, slack_ms: int = 120_000) -> None:
    def is_bot(f):
        if f["client"].startswith(APP_PREFIXES):
            return False
        return any(sym in (trip.symbol, None) and a - slack_ms <= f["time"] <= b + slack_ms
                   for sym, a, b, _ in windows)
    qty = sum(f["qty"] for f in trip.fills)
    bot = sum(f["qty"] for f in trip.fills if is_bot(f))
    trip.owner = "bot" if qty and bot / qty > 0.5 else "manual"


# ------------------------------------------------------------------ report
def collect(ex, cfg: dict, days: int, now_ms: int | None = None) -> dict:
    now_ms = now_ms or int(time.time() * 1000)
    since = now_ms - days * DAY_MS
    hist = now_ms - max(days, LOOKBACK_DAYS) * DAY_MS
    income = fetch_income(ex, hist, now_ms)
    held = {p["info"]["symbol"] if p.get("info", {}).get("symbol") else _raw(p["symbol"]):
            float(p["contracts"]) * (1 if p["side"] == "long" else -1)
            for p in ex.fetch_positions() if float(p.get("contracts") or 0)}
    first: dict[str, int] = {}  # each coin's first fill (commission) in the history: fetch from there
    for r in income:
        if r.get("symbol") and r["incomeType"] in ("REALIZED_PNL", "COMMISSION"):
            first[r["symbol"]] = min(first.get(r["symbol"], now_ms), int(r["time"]))
    for sym in held:
        first.setdefault(sym, hist)
    windows = bot_windows(cfg, now_ms)
    trips = []
    for sym in sorted(first):
        ts = build_trips(fetch_fills(ex, sym, max(hist, first[sym] - 60_000), now_ms), held.get(sym, 0.0))
        add_funding(ts, income)
        for t in ts:
            classify(t, windows)
        trips += ts
    transfers = sum(float(r["income"]) for r in income
                    if r["incomeType"] == "TRANSFER" and int(r["time"]) >= since)
    closed = [t for t in trips if t.closed is not None and t.closed >= since]
    return {"days": days, "closed": closed, "open": [t for t in trips if t.closed is None],
            "transfers": transfers, "windows": windows, "since": since, "income": income}


def _group(trips: list[Trip]) -> str:
    if not trips:
        return "沒有平倉"
    wins = [t.pnl for t in trips if t.pnl > 0]
    losses = [t.pnl for t in trips if t.pnl <= 0]
    total = sum(t.pnl for t in trips)
    line = f"{len(trips)} 筆，賺 {len(wins)} 筆（{len(wins) / len(trips):.0%}），合計 {total:+.2f} U"
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    line += f"\n    平均賺 {aw:+.2f}｜平均賠 {al:+.2f}"
    if wins and losses and al:
        line += f"｜盈虧比 {abs(aw / al):.2f}"
    worst = min(trips, key=lambda t: t.pnl)
    if worst.pnl < 0:
        when = datetime.fromtimestamp(worst.closed / 1000, timezone.utc).strftime("%m/%d")
        line += f"\n    最大虧損 {worst.pnl:+.2f} U（{worst.coin} {when}）"
    return line


def format_pnl(data: dict) -> str:
    bot = [t for t in data["closed"] if t.owner == "bot"]
    man = [t for t in data["closed"] if t.owner == "manual"]
    lines = [f"📒 已實現損益（最近 {data['days']} 天，已扣手續費與資金費率，不含轉帳）",
             f"🤖 機器人：{_group(bot)}"]
    rs = [r for sym, a, b, r in data["windows"] if r == r and b >= data["since"]]
    if rs:
        lines.append(f"    平均每筆 {sum(rs) / len(rs):+.2f}R（機器人紀錄 {len(rs)} 筆）")
    lines.append(f"✋ 手動：{_group(man)}")
    if data["open"]:
        part = sum(t.pnl for t in data["open"])
        lines.append("📌 未平倉（全部平倉後才算進上面）：" + "、".join(
            f"{'🤖' if t.owner == 'bot' else '✋'}{t.coin}" for t in data["open"])
            + (f"\n    其中已部分平倉、手續費、資金費率合計 {part:+.2f} U" if abs(part) > 0.005 else ""))
    if abs(data["transfers"]) > 0.005:
        lines.append(f"💸 轉入轉出 {data['transfers']:+.2f} U（不算損益）")
    lines.append("（手動單沒有事先記錄停損，無法算 R，用盈虧比代替）")
    return "\n".join(lines)
