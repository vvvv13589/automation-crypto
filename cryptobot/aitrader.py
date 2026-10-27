"""AI discretionary trader: Claude reads the top-50 crypto market, the biggest TradFi perps
(stocks, gold, oil) and the news, and decides.

    python3 -m cryptobot ai-trader                 # paper only (200 U simulated)
    python3 -m cryptobot ai-trader --live          # AI sub-account for real + the paper mirror
    python3 -m cryptobot ai-trader --check         # read-only: is the live key the AI sub-account?
    python3 -m cryptobot ai-trader --live-test DOGE # smallest real round trip: open, stop, tp, close
    python3 -m cryptobot ai-trader --once          # one paper decision now, then exit

The AI only *proposes* actions as JSON; every action passes hard limits in code (Guard) that it
cannot change: a stop on every trade, max risk per trade, max account leverage, max positions,
stops may only be tightened, and no new trades below the equity floor. Live stops and
take-profits rest on the exchange. Paper fills use live Binance prices (taker fee + slippage,
real funding at 00/08/16 UTC). Live keys: AITRADER_API_KEY / AITRADER_API_SECRET.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import AI_ROOT, ClaudeChat

log = logging.getLogger(__name__)

DEFAULTS = {
    "paper_cash": 200.0,  # simulated account, mirrors every decision
    "live_cash": 50.0,  # what was put into the AI sub-account (for % and the target)
    "target_multiple": 5,
    "live_max_equity": 120.0,  # refuse to run on an account bigger than this (wrong API key?)
    "model": "opus",
    "max_turns": 25,
    "decide_minutes": 60,  # regular decision every hour
    "urgent_gap_minutes": 30,  # breaking news may trigger an extra decision, at most this often
    "event_triggers": True,  # an open position first reaching +1R / +2R, or moving >= event_move_r in 15 min,
    "event_gap_minutes": 15,  # wakes the AI for an extra decision (at most this often)
    "event_move_r": 1.0,
    "news_minutes": 5,
    "universe": 50,
    "tradfi": 15,  # also scan the top TradFi perps (stocks, gold, oil...) by volume; 0 = crypto only
    "tradfi_min_volume": 50e6,  # 24 h quote volume, USDT
    "session_buffer_minutes": 30,  # no new stock positions this close to (or after) the home market's close
    "max_positions": 4,
    "max_risk_pct": 5.0,  # loss at the stop, % of equity, per trade
    "max_leverage": 5.0,  # total open notional / equity
    "floor_frac": 0.5,  # no new positions at or below this share of the starting cash
    "daily_loss_pct": 10.0,  # no new positions for the rest of the Taipei day after losing this much
    "max_plans": 6,  # conditional entries waiting at the same time
    "plan_max_hours": 72,
    "min_stop_pct": 0.3,
    "max_stop_pct": 25.0,
    "taker_fee": 0.0005,
    "slippage": 0.0003,
    "daily_report": "09:00",
    "weekly_review": "Sun 10:00",  # the AI reviews the week's closed trades and rewrites its lessons; "" = off
    "lessons_max": 10,
    "timezone": "Asia/Taipei",
}
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "XUSD", "EUR"}
FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
    # traditional markets (stocks, gold, oil) for the TradFi perps
    "CNBC Markets": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "Investing 原物料": "https://www.investing.com/rss/news_11.rss",
    "Investing 股市": "https://www.investing.com/rss/news_25.rss",
    "OilPrice": "https://oilprice.com/rss/main",
}
BINANCE_NEWS = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                "?type=1&pageNo=1&pageSize=10")
URGENT = re.compile(r"hack|exploit|drain|stolen|delist|liquidat|crash|plunge|sec\b|etf|"
                    r"fed\b|fomc|rate (cut|hike)|cpi|tariff|bankrupt|halt|outage|depeg|lawsuit|ban\b|"
                    r"approv|will list|launchpool|emergency|opec|payroll|powell|"
                    r"guidance cut|profit warning", re.I)
UA = {"User-Agent": "Mozilla/5.0 (cryptobot ai-trader)"}


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("aitrader") or {})}


def _get(url: str, timeout: int = 15) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


TW = ZoneInfo("Asia/Taipei")  # every time the AI and the user see is Taipei time (UTC+8)


def tw(ts: float) -> str:
    return datetime.fromtimestamp(ts, TW).strftime("%Y-%m-%d %H:%M")


def fmt(x: float) -> str:
    return f"{x:.6g}"


# -- news -----------------------------------------------------------------------
class News:
    """Headlines from crypto RSS feeds and Binance announcements, kept 48 h."""

    def __init__(self, path: Path, fetch=_get, clock=time.time):
        self.path, self.fetch, self.clock = path, fetch, clock
        self.items: list[dict] = json.loads(path.read_text()) if path.exists() else []

    def _parse_rss(self, source: str, raw: bytes) -> list[dict]:
        out = []
        for it in ET.fromstring(raw).iter("item"):
            title = (it.findtext("title") or "").strip()
            when = it.findtext("pubDate") or ""
            try:
                ts = datetime.strptime(when.strip()[:25], "%a, %d %b %Y %H:%M:%S").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                ts = self.clock()
            if title:
                out.append({"src": source, "title": title, "ts": ts})
        return out

    def poll(self) -> list[dict]:
        """Fetch every source; return the headlines not seen before."""
        got: list[dict] = []
        for name, url in FEEDS.items():
            try:
                got += self._parse_rss(name, self.fetch(url))
            except Exception as exc:
                log.warning("news %s: %s", name, str(exc)[:120])
        try:
            data = json.loads(self.fetch(BINANCE_NEWS))
            for cat in data["data"]["catalogs"]:
                for a in cat.get("articles", []):
                    got.append({"src": f"Binance/{cat.get('catalogName', '')}", "title": a["title"],
                                "ts": a.get("releaseDate", 0) / 1000})
        except Exception as exc:
            log.warning("news binance: %s", str(exc)[:120])
        seen = {i["title"] for i in self.items}
        cutoff = self.clock() - 48 * 3600
        new = [g for g in got if g["title"] not in seen and g["ts"] > cutoff]
        for g in new:
            seen.add(g["title"])
        self.items = sorted([i for i in self.items + new if i["ts"] > cutoff], key=lambda i: -i["ts"])
        self.path.write_text(json.dumps(self.items, ensure_ascii=False))
        return new

    def digest(self, limit: int = 60) -> str:
        if not self.items:
            return "（暫時抓不到新聞）"
        return "\n".join(f"{tw(i['ts'])} [{i['src']}] {i['title']}" for i in self.items[:limit])

    @staticmethod
    def urgent(items: list[dict]) -> list[dict]:
        return [i for i in items if URGENT.search(i["title"])]


# -- market sessions (TradFi perps trade 24/7, but their price is only discovered while the home
# market is open; outside it they drift on thin volume and can gap at the open). Holidays ignored.
SESSIONS = {  # underlyingType -> (label, timezone, [(open, close)], weekdays)
    "EQUITY": ("美股", "America/New_York", [("09:30", "16:00")]),
    "KR_EQUITY": ("韓股", "Asia/Seoul", [("09:00", "15:30")]),
    "HK_EQUITY": ("港股", "Asia/Hong_Kong", [("09:30", "12:00"), ("13:00", "16:00")]),
    "CN_EQUITY": ("A股", "Asia/Shanghai", [("09:30", "11:30"), ("13:00", "15:00")]),
}
TRADFI_KINDS = set(SESSIONS) | {"COMMODITY"}


def session(kind: str, now: float) -> tuple[bool, float, str]:
    """(open, minutes until close (0 when closed), short label) for an underlying type at `now`."""
    if kind in ("COIN", "", None):
        return True, 1e9, ""
    if kind == "COMMODITY":  # CME Globex: Sunday 18:00 – Friday 17:00 New York, daily break 17:00–18:00
        t = datetime.fromtimestamp(now, ZoneInfo("America/New_York"))
        m = t.hour * 60 + t.minute
        wd = t.weekday()  # Monday 0 … Sunday 6
        closed = wd == 5 or (wd == 6 and m < 18 * 60) or (wd == 4 and m >= 17 * 60) or 17 * 60 <= m < 18 * 60
        if closed:
            return False, 0.0, "期貨·休市"
        return True, (17 * 60 - m) % (24 * 60) or 24 * 60, "期貨·交易中"
    label, tzname, windows = SESSIONS.get(kind, (kind, "UTC", [("00:00", "23:59")]))
    t = datetime.fromtimestamp(now, ZoneInfo(tzname))
    m = t.hour * 60 + t.minute
    if t.weekday() < 5:
        for o, c in windows:
            om, cm = int(o[:2]) * 60 + int(o[3:]), int(c[:2]) * 60 + int(c[3:])
            if om <= m < cm:
                return True, float(cm - m), f"{label}·開盤"
    return False, 0.0, f"{label}·休市"


# -- market -----------------------------------------------------------------------
def ema(values: list[float], n: int) -> float:
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


class Market:
    def __init__(self, ex, size: int = 50, tradfi: int = 0, tradfi_min_volume: float = 0.0, clock=time.time):
        self.ex, self.size, self.tradfi, self.tradfi_min_volume, self.clock = ex, size, tradfi, tradfi_min_volume, clock
        self.tickers: dict = {}
        self.kinds: dict[str, str] = {}  # base -> underlyingType ("COIN", "EQUITY", "COMMODITY", ...)

    def universe(self) -> list[str]:
        """Top crypto USDT perpetuals by 24 h quote volume (no stablecoins), then the top TradFi
        perpetuals (stocks, ETFs, gold, oil; no pre-IPO or FX) above a volume floor."""
        self.tickers = self.ex.fetch_tickers()
        crypto, tradfi = [], []
        for sym, t in self.tickers.items():
            m = self.ex.markets.get(sym) or {}
            info = m.get("info") or {}
            if not (m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)):
                continue
            ctype, kind = info.get("contractType", "PERPETUAL"), info.get("underlyingType", "COIN")
            vol = float(t.get("quoteVolume") or 0)
            if ctype == "PERPETUAL" and kind == "COIN" and m.get("base") not in STABLES:
                crypto.append((vol, m["base"], kind))
            elif ctype == "TRADIFI_PERPETUAL" and kind in TRADFI_KINDS and vol >= self.tradfi_min_volume:
                tradfi.append((vol, m["base"], kind))
        rows = sorted(crypto, reverse=True)[: self.size] + sorted(tradfi, reverse=True)[: self.tradfi]
        self.kinds = {b: k for _, b, k in rows}
        return [b for _, b, _ in rows]

    def session(self, coin: str) -> tuple[bool, float, str]:
        kind = self.kinds.get(coin)
        if kind is None:  # not scanned yet (e.g. a plan right after a restart): ask the market list
            info = (self.ex.markets.get(f"{coin}/USDT:USDT") or {}).get("info") or {}
            kind = info.get("underlyingType", "COIN")
        return session(kind, self.clock())

    def price(self, coin: str) -> float:
        t = self.ex.fetch_ticker(f"{coin}/USDT:USDT")
        return float(t["last"])

    def funding(self) -> dict[str, float]:
        try:
            rates = self.ex.fetch_funding_rates()
        except Exception as exc:
            log.warning("funding: %s", exc)
            return {}
        return {s.split("/")[0]: float(r.get("fundingRate") or 0) for s, r in rates.items()}

    def fetch_coin(self, coin: str) -> dict | None:
        """Everything the scan needs for one coin: 1h / 4h / 1d candles and 1h open interest."""
        sym = f"{coin}/USDT:USDT"
        try:
            d = {"h1": self.ex.fetch_ohlcv(sym, "1h", limit=48), "h4": self.ex.fetch_ohlcv(sym, "4h", limit=60),
                 "d1": self.ex.fetch_ohlcv(sym, "1d", limit=60)}
        except Exception as exc:
            log.warning("klines %s: %s", coin, exc)
            return None
        try:
            d["oi"] = [float(r.get("openInterestValue") or 0)
                       for r in self.ex.fetch_open_interest_history(sym, "1h", limit=25)]
        except Exception as exc:
            log.warning("oi %s: %s", coin, exc)
            d["oi"] = []
        d["ls"] = self.fetch_ratios(sym)
        return d

    def fetch_ratios(self, sym: str) -> dict:
        """Binance public futures data: top-trader position long/short ratio (the biggest 20% of
        accounts by margin), all-account long/short ratio, taker buy/sell volume ratio (1h)."""
        out = {}
        try:
            p = {"symbol": self.ex.market(sym)["id"], "period": "1h"}
            out["top"] = [float(r["longShortRatio"]) for r in
                          self.ex.fapiDataGetTopLongShortPositionRatio({**p, "limit": 25})]
            out["retail"] = [float(r["longShortRatio"]) for r in
                             self.ex.fapiDataGetGlobalLongShortAccountRatio({**p, "limit": 25})]
            out["taker"] = [(float(r["buyVol"]), float(r["sellVol"])) for r in
                            self.ex.fapiDataGetTakerlongshortRatio({**p, "limit": 4})]
        except Exception as exc:
            log.warning("long/short ratios %s: %s", sym, exc)
        return out

    def digest(self, coins: list[str]) -> str:
        """Full scan of every coin (no AI): the AI gets all rows, plus the coins the screens flag."""
        fund = self.funding()
        raw = {c: self.fetch_coin(c) for c in coins}
        btc = raw.get("BTC")
        btc7 = change(btc["h4"], 42) if btc else 0.0
        rows = []
        for c in coins:
            if not raw[c]:
                continue
            s = scan_coin(raw[c], fund.get(c, 0.0), btc7)
            if s:
                rows.append((c, s))
        lines = [SCAN_HEADER]
        lines += [scan_line(c, s) for c, s in rows if self.kinds.get(c, "COIN") == "COIN"]
        trad = [(c, s) for c, s in rows if self.kinds.get(c, "COIN") != "COIN"]
        if trad:
            lines.append("\n【傳統市場 TradFi 永續（股票、ETF、黃金、原油；〔〕是本地市場現在的狀態，"
                         "休市時價格只在盤後薄量漂移、開盤可能跳空；7d相對BTC 對它們參考意義不大）】")
            lines += [scan_line(f"{c}〔{self.session(c)[2]}〕", s) for c, s in trad]
        # trend alignment alone is common (most coins drift with BTC): list coins with an actual event
        flagged = sorted((r for r in rows if any(not f.startswith("日線與") for f in r[1]["flags"])),
                         key=lambda r: -len(r[1]["flags"]))
        lines.append("\n【程式篩選出的訊號（客觀條件，不代表一定要做）】")
        lines += [f"{c}：{'、'.join(s['flags'])}" for c, s in flagged] or ["（這小時沒有幣觸發篩選條件）"]
        return "\n".join(lines)


SCAN_HEADER = ("幣種 | 價格 | 24h% | 7d% | 30d% | 7d相對BTC | 日線結構 | 4h結構 | 4h EMA50 | 距20日高/低% | "
               "24h區間位置 | 持倉量24h% | 量能倍數 | 資金費率%/8h | ATR(4h)% | 大戶多空比(24h變化) | "
               "散戶多空比 | 主動買賣比4h")


def change(bars: list, n: int) -> float:
    """% change over the last n bars."""
    if len(bars) <= n:
        return 0.0
    return (bars[-1][4] / bars[-1 - n][4] - 1) * 100


def structure(bars: list, n: int) -> str:
    """升 = higher high and higher low than the n bars before, 降 = both lower, else 盤."""
    if len(bars) < 2 * n:
        return "?"
    rec, prev = bars[-n:], bars[-2 * n:-n]
    h1, h0 = max(b[2] for b in rec), max(b[2] for b in prev)
    l1, l0 = min(b[3] for b in rec), min(b[3] for b in prev)
    return "升" if h1 > h0 and l1 > l0 else "降" if h1 < h0 and l1 < l0 else "盤"


def scan_coin(d: dict, funding: float, btc7: float) -> dict | None:
    h1, h4, d1, oi = d["h1"], d["h4"], d["d1"], d.get("oi") or []
    if len(h4) < 50 or len(d1) < 21 or len(h1) < 24:
        return None
    last = h4[-1][4]
    trs = [max(h4[i][2] - h4[i][3], abs(h4[i][2] - h4[i - 1][4]), abs(h4[i][3] - h4[i - 1][4]))
           for i in range(len(h4) - 14, len(h4))]
    atr = sum(trs) / 14
    hi20 = max(b[2] for b in d1[-21:-1])  # 20 finished days, today excluded
    lo20 = min(b[3] for b in d1[-21:-1])
    day_hi, day_lo = max(b[2] for b in h1[-24:]), min(b[3] for b in h1[-24:])
    vol24 = sum(b[5] * b[4] for b in h1[-24:])
    vol_avg = sum(b[5] * b[4] for b in d1[-8:-1]) / 7 if len(d1) >= 8 else 0
    s = {
        "price": last, "c24": change(h1, 24), "c7": change(h4, 42), "c30": change(d1, 30) if len(d1) > 30 else 0.0,
        "d_struct": structure(d1, 10), "h4_struct": structure(h4, 20),
        "ema": (last / ema([b[4] for b in h4], 50) - 1) * 100,
        "to_hi": (last / hi20 - 1) * 100, "to_lo": (last / lo20 - 1) * 100,
        "range_pos": (last - day_lo) / (day_hi - day_lo) * 100 if day_hi > day_lo else 50.0,
        "oi24": (oi[-1] / oi[0] - 1) * 100 if len(oi) >= 2 and oi[0] else None,
        "vol_x": vol24 / vol_avg if vol_avg else None, "funding": funding * 100, "atr": atr / last * 100,
    }
    s["rs"] = s["c7"] - btc7
    ls = d.get("ls") or {}
    top, retail, taker = ls.get("top") or [], ls.get("retail") or [], ls.get("taker") or []
    s["top"] = top[-1] if top else None
    s["top_chg"] = top[-1] - top[0] if len(top) >= 2 else None
    s["retail"] = retail[-1] if retail else None
    sells = sum(v for _, v in taker)
    s["taker"] = sum(b for b, _ in taker) / sells if sells else None
    flags = []
    if last > hi20:
        flags.append("站上 20 日高點")
    elif hi20 - last <= atr:
        flags.append("貼近 20 日高點")
    if last < lo20:
        flags.append("跌破 20 日低點")
    elif last - lo20 <= atr:
        flags.append("貼近 20 日低點")
    if s["oi24"] is not None and abs(s["oi24"]) >= 15:
        flags.append(f"持倉量 24h {s['oi24']:+.0f}%")
    if s["oi24"] is not None and s["oi24"] >= 8 and abs(s["c24"]) >= 3:
        flags.append("價漲量增" if s["c24"] > 0 else "價跌量增")
    if s["vol_x"] is not None and s["vol_x"] >= 2:
        flags.append(f"成交量 {s['vol_x']:.1f} 倍")
    if abs(s["funding"]) >= 0.05:
        flags.append(f"資金費率偏{'多' if s['funding'] > 0 else '空'} {s['funding']:+.3f}%")
    if abs(s["rs"]) >= 10:
        flags.append(f"7d {'強' if s['rs'] > 0 else '弱'}於 BTC {s['rs']:+.0f}%")
    if s["top"] is not None and s["retail"] is not None:
        if s["top"] < 1 and s["retail"] > 1.3:
            flags.append(f"大戶偏空、散戶偏多（{s['top']:.2f} vs {s['retail']:.2f}）")
        elif s["top"] > 1.3 and s["retail"] < 1:
            flags.append(f"大戶偏多、散戶偏空（{s['top']:.2f} vs {s['retail']:.2f}）")
    if s["top_chg"] is not None and s["top"] and abs(s["top_chg"]) / (s["top"] - s["top_chg"] or 1) >= 0.25:
        flags.append(f"大戶多空比 24h {s['top'] - s['top_chg']:.2f}→{s['top']:.2f}")
    if s["taker"] is not None and (s["taker"] >= 1.3 or s["taker"] <= 0.77):
        flags.append(f"主動{'買' if s['taker'] > 1 else '賣'}盤強（4h 買賣比 {s['taker']:.2f}）")
    if s["d_struct"] != "盤" and s["d_struct"] == s["h4_struct"]:
        flags.append(f"日線與 4h 同步{'上升' if s['d_struct'] == '升' else '下降'}")
    s["flags"] = flags
    return s


def scan_line(c: str, s: dict) -> str:
    oi = f"{s['oi24']:+.1f}" if s["oi24"] is not None else "?"
    vx = f"{s['vol_x']:.1f}" if s["vol_x"] is not None else "?"
    return (f"{c} | {fmt(s['price'])} | {s['c24']:+.1f} | {s['c7']:+.1f} | {s['c30']:+.1f} | {s['rs']:+.1f} | "
            f"{s['d_struct']} | {s['h4_struct']} | {s['ema']:+.1f}% | {s['to_hi']:+.1f}/{s['to_lo']:+.1f} | "
            f"{s['range_pos']:.0f}% | {oi} | {vx} | {s['funding']:+.4f} | {s['atr']:.1f} | "
            f"{ratio(s['top'])}({'+' if (s['top_chg'] or 0) >= 0 else ''}{ratio(s['top_chg'])}) | "
            f"{ratio(s['retail'])} | {ratio(s['taker'])}")


def ratio(x) -> str:
    return "?" if x is None else f"{x:.2f}"



# -- accounts: paper book, live book, hard limits -----------------------------------------
class LiveError(Exception):
    """A live order could not be done; the message is shown to the user and the AI."""


class Book:
    """Simulated USDT-perpetual account. State survives restarts (JSON)."""

    live = False
    label = "模擬"

    def __init__(self, path: Path, trades_csv: Path, cfg: dict, starting_cash: float, clock=time.time):
        self.path, self.trades_csv, self.cfg, self.clock = path, trades_csv, cfg, clock
        self.starting_cash = starting_cash
        self.floor = starting_cash * cfg["floor_frac"]
        s = json.loads(path.read_text()) if path.exists() else {}
        self.cash: float = s.get("cash", starting_cash)
        self.positions: dict[str, dict] = s.get("positions", {})
        self.started: float = s.get("started", clock())
        self.last_check: float = s.get("last_check", clock())
        self.last_funding = s.get("last_funding")
        self.btc_start = s.get("btc_start")
        self.day, self.day_start = s.get("day"), s.get("day_start")

    def save(self) -> None:
        self.path.write_text(json.dumps({"cash": self.cash, "positions": self.positions, "started": self.started,
                                         "last_check": self.last_check, "last_funding": self.last_funding,
                                         "btc_start": self.btc_start, "day": self.day, "day_start": self.day_start},
                                        indent=1))

    def mark_day(self, now: float, equity) -> None:
        """Equity at the start of each Taipei day (the daily loss limit's reference)."""
        day = datetime.fromtimestamp(now, TW).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start = day, equity()
            self.save()

    def day_loss_pct(self, eq: float) -> float:
        return (1 - eq / self.day_start) * 100 if self.day_start else 0.0

    def upnl(self, prices: dict[str, float]) -> float:
        return sum(p["qty"] * (prices.get(c, p["entry"]) - p["entry"]) * (1 if p["side"] == "long" else -1)
                   for c, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.upnl(prices)

    def notional(self, prices: dict[str, float]) -> float:
        return sum(p["qty"] * prices.get(c, p["entry"]) for c, p in self.positions.items())

    def _new(self, coin, side, qty, entry, stop, tp, reason, now, fee, **extra) -> dict:
        self.positions[coin] = {"side": side, "qty": qty, "entry": entry, "stop": stop, "tp": tp, "opened": now,
                                "reason": reason, "fees": fee, "funding": 0.0, "init_stop": stop, **extra}
        self.save()
        return self.positions[coin]

    def _record(self, coin: str, p: dict, exit_px: float, exit_fee: float, why: str, now: float) -> dict:
        sgn = 1 if p["side"] == "long" else -1
        net = (exit_px - p["entry"]) * p["qty"] * sgn - exit_fee - p["fees"] + p["funding"]
        risk = abs(p["entry"] - p["init_stop"]) * p["qty"]
        row = {"coin": coin, "side": p["side"], "opened": tw(p["opened"]), "closed": tw(now),
               "entry": fmt(p["entry"]), "exit": fmt(exit_px), "qty": fmt(p["qty"]), "pnl": round(net, 2),
               "r": round(net / risk, 2) if risk else 0, "why": why, "reason": p["reason"]}
        new = not self.trades_csv.exists()
        with self.trades_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
        self.save()
        return row

    def open(self, coin: str, side: str, price: float, qty: float, stop: float, tp: float | None,
             reason: str, now: float) -> dict:
        fill = price * (1 + self.cfg["slippage"] * (1 if side == "long" else -1))
        fee = fill * qty * self.cfg["taker_fee"]
        self.cash -= fee
        return self._new(coin, side, qty, fill, stop, tp, reason, now, fee)

    def close(self, coin: str, price: float, why: str, now: float, slip: bool = True) -> dict:
        p = self.positions.pop(coin)
        sgn = 1 if p["side"] == "long" else -1
        fill = price * (1 - self.cfg["slippage"] * sgn) if slip else price
        fee = fill * p["qty"] * self.cfg["taker_fee"]
        self.cash += (fill - p["entry"]) * p["qty"] * sgn - fee
        return self._record(coin, p, fill, fee, why, now)

    def move_stop(self, coin: str, stop: float, now: float) -> None:
        self.positions[coin].update(stop=stop, since=now)
        self.save()

    def set_tp(self, coin: str, tp: float | None) -> None:
        self.positions[coin]["tp"] = tp
        self.save()

    def apply_funding(self, rates: dict[str, float], prices: dict[str, float]) -> None:
        for c, p in self.positions.items():
            amt = p["qty"] * prices.get(c, p["entry"]) * rates.get(c, 0) * (-1 if p["side"] == "long" else 1)
            p["funding"] += amt
            self.cash += amt
        self.save()

    def sync_exits(self, ex, now: float) -> list[tuple[dict, str]]:
        """Stops / take-profits hit by any 1m candle since the last check."""
        closed = []
        for coin, p in list(self.positions.items()):
            # only whole minutes after the entry / last stop change: earlier prices never applied
            start = max(self.last_check - 60, p.get("since", p["opened"]))
            first = int(-(-p.get("since", p["opened"]) // 60) * 60_000)
            try:
                bars = ex.fetch_ohlcv(f"{coin}/USDT:USDT", "1m", since=int(start // 60 * 60_000), limit=500)
                bars = [b for b in bars if b[0] >= first]
            except Exception as exc:
                log.warning("1m %s: %s", coin, exc)
                continue
            long = p["side"] == "long"
            for _, o, h, l, _c, _v in bars:
                stop_hit = (l <= p["stop"]) if long else (h >= p["stop"])
                tp_hit = p["tp"] is not None and ((h >= p["tp"]) if long else (l <= p["tp"]))
                if stop_hit:  # worst case first when both are inside one candle
                    gap = (o < p["stop"]) if long else (o > p["stop"])
                    closed.append((self.close(coin, o if gap else p["stop"], "停損", now), "🛑"))
                    break
                if tp_hit:
                    closed.append((self.close(coin, p["tp"], "止盈", now, slip=False), "🎯"))
                    break
        self.last_check = now
        self.save()
        return closed


class LiveBook(Book):
    """A real Binance USDT-M account (meant to be a sub-account used only by the AI trader).
    Stops and take-profits rest on the exchange, so the position is protected even if this
    process or the VM is down. A position is never left without a stop: if placing it fails,
    the position is closed at once."""

    live = True
    label = "真倉"

    def __init__(self, path: Path, trades_csv: Path, cfg: dict, starting_cash: float, ex, clock=time.time,
                 broker_factory=None):
        super().__init__(path, trades_csv, cfg, starting_cash, clock)
        self.ex = ex
        if broker_factory is None:
            from .broker import FuturesLiveBroker
            broker_factory = lambda sym: FuturesLiveBroker(ex, sym, int(cfg["max_leverage"]), "isolated")
        self._factory, self._brokers = broker_factory, {}

    def broker(self, coin: str):
        if coin not in self._brokers:
            self._brokers[coin] = self._factory(f"{coin}/USDT:USDT")
        return self._brokers[coin]

    def equity(self, prices: dict[str, float]) -> float:
        return self.broker("BTC").equity(0)

    def exchange_positions(self) -> dict[str, float]:
        out = {}
        for p in self.ex.fetch_positions():
            amt = float(p.get("contracts") or 0) * float(p.get("contractSize") or 1)
            if amt:
                out[p["symbol"].split("/")[0]] = amt if p.get("side") == "long" else -amt
        return out

    def check_account(self) -> str | None:
        """Refuse to trade an account that is not the AI's own (e.g. the main bot's keys)."""
        eq = self.equity({})
        if eq < 10:
            return (f"U 本位合約錢包只有 {eq:.2f} U：請把 USDT 劃轉到這個子帳戶的「U 本位合約」錢包"
                    "（不是現貨錢包）")
        if eq > self.cfg["live_max_equity"]:
            return (f"帳戶權益 {eq:.2f} U 超過 live_max_equity {self.cfg['live_max_equity']:g} U："
                    "這可能不是 AI 專用的子帳戶，拒絕啟動")
        unknown = sorted(set(self.exchange_positions()) - set(self.positions))
        if unknown:
            return f"帳戶裡有不是 AI 開的倉位 {', '.join(unknown)}：這可能不是 AI 專用的子帳戶，拒絕啟動"
        return None

    def _place_tp(self, b, side: str, amount: float, tp: float) -> str:
        price = float(self.ex.price_to_precision(b.symbol, tp))
        order = self.ex.create_order(b.symbol, "market", "sell" if side == "long" else "buy", b._amount(amount),
                                     None, {"takeProfitPrice": price, "reduceOnly": True})
        return order["id"]

    def open(self, coin, side, price, qty, stop, tp, reason, now) -> dict:
        b = self.broker(coin)
        dist = abs(price - stop) / price
        # isolated leverage low enough that liquidation lies well beyond the stop
        lev = int(max(1, min(self.cfg["max_leverage"], 1 / (dist + 0.02))))
        free = b.free_margin(price)
        qty = min(qty, free * 0.9 * lev / price)
        limits = (self.ex.market(b.symbol).get("limits") or {}).get("cost") or {}
        too_small = LiveError(f"數量太小，低於交易所最低下單（{qty * price:.1f} U，最低 {limits.get('min')} U，"
                              f"可用保證金 {free:.2f} U）")
        b.use_leverage(lev)
        try:
            fill = b.market_open(side, qty, price)
        except Exception as exc:
            if "precision" in str(exc) or "minimum" in str(exc):  # rounds down to zero
                raise too_small from exc
            raise
        if fill is None or fill.amount <= 0:
            raise too_small
        try:
            stop_id = b.set_stop(side, fill.amount, stop)
        except Exception as exc:
            b.market_close(side, fill.amount, price)
            raise LiveError(f"停損掛單失敗，已立刻市價平倉：{str(exc)[:150]}")
        tp_id = None
        if tp:
            try:
                tp_id = self._place_tp(b, side, fill.amount, tp)
            except Exception as exc:
                log.warning("%s take-profit failed: %s", coin, exc)
                tp = None
        return self._new(coin, side, fill.amount, fill.price, stop, tp, reason, now, fill.fee,
                         stop_id=stop_id, tp_id=tp_id, leverage=lev)

    def close(self, coin, price, why, now, slip=True) -> dict:
        p = self.positions[coin]
        b = self.broker(coin)
        b.cancel_stop(p.get("stop_id"))
        b.cancel_stop(p.get("tp_id"))
        since = datetime.fromtimestamp(p["opened"], timezone.utc).isoformat()
        fill = b.market_close(p["side"], p["qty"], price, since=since)
        self.positions.pop(coin)
        exit_px = fill.price if fill else price
        if why is None:  # closed on the exchange: by the stop or by the take-profit order
            near_tp = p.get("tp") and abs(exit_px - p["tp"]) < abs(exit_px - p["stop"])
            why = "止盈" if near_tp else "停損"
        return self._record(coin, p, exit_px, fill.fee if fill else 0.0, why, now)

    def move_stop(self, coin, stop, now) -> None:
        p = self.positions[coin]
        p["stop_id"] = self.broker(coin).set_stop(p["side"], p["qty"], stop, p.get("stop_id"))
        super().move_stop(coin, stop, now)

    def set_tp(self, coin, tp) -> None:
        p = self.positions[coin]
        b = self.broker(coin)
        new_id = self._place_tp(b, p["side"], p["qty"], tp) if tp else None
        b.cancel_stop(p.get("tp_id"))
        p["tp_id"] = new_id
        super().set_tp(coin, tp)

    def apply_funding(self, rates, prices) -> None:
        pass  # real funding is already in the exchange balance

    def sync_exits(self, ex, now) -> list[tuple[dict, str]]:
        """Positions the exchange closed (stop / take-profit / liquidation / manual)."""
        if not self.positions:
            return []
        try:
            live = self.exchange_positions()
        except Exception as exc:
            log.warning("positions: %s", exc)
            return []
        closed = []
        for coin in [c for c in self.positions if c not in live]:
            row = self.close(coin, self.positions[coin]["stop"], None, now)
            closed.append((row, "🎯" if row["why"] == "止盈" else "🛑"))
        self.last_check = now
        self.save()
        return closed



class Guard:
    """Hard limits the AI cannot override. Returns (ok, message, normalised action)."""

    def __init__(self, cfg: dict, session=None):
        self.cfg = cfg
        self.session = session  # coin -> (open, minutes to close, label); None = always open

    def check_open(self, a: dict, book: Book, price: float, prices: dict, universe: list[str]):
        c = self.cfg
        coin, side = str(a.get("coin", "")).upper(), a.get("side")
        if coin not in universe:
            return False, f"{coin} 不在前 {c['universe']} 大", None
        if self.session:
            is_open, left, label = self.session(coin)
            if not is_open:
                return False, f"{coin} 本地市場休市（{label}），不開新倉（開盤跳空風險）", None
            if left < c["session_buffer_minutes"]:
                return False, f"{coin} 離收盤只剩 {left:.0f} 分鐘，不開新倉（隔夜跳空風險）", None
        if coin in book.positions:
            return False, f"{coin} 已有倉位（不加倉）", None
        if len(book.positions) >= c["max_positions"]:
            return False, f"已達 {c['max_positions']} 個倉位上限", None
        if side not in ("long", "short"):
            return False, "side 要是 long/short", None
        eq = book.equity(prices)
        if eq <= book.floor:
            return False, f"權益 {eq:.1f} ≤ 底線 {book.floor:g}，停止開新倉", None
        if book.day_loss_pct(eq) >= c["daily_loss_pct"]:
            return False, (f"今天已虧 {book.day_loss_pct(eq):.1f}%（上限 {c['daily_loss_pct']:g}%），"
                           "台北時間明天 00:00 前不開新倉"), None
        try:
            stop = float(a["stop"])
        except (KeyError, TypeError, ValueError):
            return False, "沒有停損，拒絕", None
        dist = (price - stop) / price * (1 if side == "long" else -1)
        if dist <= 0:
            return False, f"停損 {fmt(stop)} 在錯的一邊（現價 {fmt(price)}）", None
        if not c["min_stop_pct"] / 100 <= dist <= c["max_stop_pct"] / 100:
            return False, f"停損距離 {dist * 100:.2f}% 不在 {c['min_stop_pct']}–{c['max_stop_pct']}%", None
        tp = a.get("take_profit")
        try:
            tp = float(tp) if tp not in (None, "", 0) else None
        except (TypeError, ValueError):
            tp = None
        if tp is not None and (tp - price) * (1 if side == "long" else -1) <= 0:
            tp = None
        risk_pct = min(max(float(a.get("risk_pct") or 1), 0.1), c["max_risk_pct"])
        qty = eq * risk_pct / 100 / (price * dist)
        room = c["max_leverage"] * eq - book.notional(prices)
        note = ""
        if qty * price > room:
            if room <= eq * 0.05:
                return False, f"總槓桿已滿 {c['max_leverage']:g} 倍", None
            qty = room / price
            note = f"（受 {c['max_leverage']:g} 倍總槓桿限制縮小）"
        return True, note, {"coin": coin, "side": side, "stop": stop, "tp": tp, "qty": qty,
                            "risk_pct": qty * price * dist / eq * 100, "reason": str(a.get("reason", ""))[:300]}

    @staticmethod
    def check_stop(a: dict, book: Book, price: float):
        coin = str(a.get("coin", "")).upper()
        p = book.positions.get(coin)
        if not p:
            return False, f"{coin} 沒有倉位", None
        try:
            stop = float(a["stop"])
        except (KeyError, TypeError, ValueError):
            return False, "停損價格不對", None
        long = p["side"] == "long"
        if (stop < p["stop"]) if long else (stop > p["stop"]):
            return False, f"只能收緊停損，不能放寬（{fmt(p['stop'])} → {fmt(stop)}）", None
        if (stop >= price) if long else (stop <= price):
            return False, f"新停損 {fmt(stop)} 已穿過現價 {fmt(price)}，請改用 close", None
        return True, "", {"coin": coin, "stop": stop}


# -- the AI -----------------------------------------------------------------------
class TraderAI(ClaudeChat):
    PROMPT = "trader-prompt.md"

    def __init__(self, cfg: dict, repo_dir: str, claude_bin: str, token: str, model: str, max_turns: int, **kw):
        super().__init__(cfg, repo_dir, claude_bin, token, **kw)
        self.model, self.max_turns = model, max_turns

    def install(self) -> None:
        super().install()
        dest = self.tools_dir / self.PROMPT
        shutil.copyfile(self.repo_dir / "deploy" / "ai" / self.PROMPT, dest)
        dest.chmod(0o644)

    def command(self, prompt: str) -> list[str]:  # a fresh session for every decision
        return [self.claude_bin, "-p", prompt, "--output-format", "json", "--model", self.model,
                "--max-turns", str(self.max_turns), "--tools", "Read,Bash", "--permission-mode", "dontAsk",
                "--settings", str(self.tools_dir / "settings.json"), "--strict-mcp-config",
                "--append-system-prompt-file", str(self.tools_dir / self.PROMPT)]

    def decide(self, prompt: str) -> tuple[dict, dict]:
        data = self._call(prompt)
        return parse_decision(str(data.get("result") or "")), data

    def review(self, prompt: str) -> tuple[dict, dict]:
        """Weekly self-review: same sandbox and tools as a decision, a different JSON reply."""
        data = self._call(prompt)
        return parse_decision(str(data.get("result") or "")), data


def parse_decision(text: str) -> dict:
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for raw in reversed(blocks or [text[text.find("{"): text.rfind("}") + 1]]):
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if isinstance(d, dict):
            d.setdefault("actions", [])
            return d
    raise ValueError(f"no decision JSON in reply: {text[-300:]}")



# -- the loop -----------------------------------------------------------------------
ACTION_NAMES = {"open": "開倉", "close": "平倉", "move_stop": "移動停損", "take_profit": "改止盈",
                "plan": "條件單", "cancel_plan": "取消條件單"}


class AITrader:
    """One AI decision per round, applied to every book: books[0] is the one the AI sees and the
    user is told about (the live account when live); the others mirror it silently (paper)."""

    def __init__(self, cfg: dict, ex, ai, notify=None, clock=time.time, fetch=_get, books=None):
        self.cfg = settings(cfg)
        state, logs = Path(cfg.get("state_dir", "state")), Path(cfg.get("log_dir", "logs"))
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        self.ex, self.ai, self.clock = ex, ai, clock
        self.notify = notify or (lambda text: None)
        self.market = Market(ex, self.cfg["universe"], self.cfg["tradfi"], self.cfg["tradfi_min_volume"], clock)
        self.books = books or [Book(state / "aitrader_book.json", logs / "aitrader_trades.csv", self.cfg,
                                    self.cfg["paper_cash"], clock)]
        self.book = self.books[0]
        self.news = News(state / "aitrader_news.json", fetch=fetch, clock=clock)
        self.journal = logs / "aitrader_decisions.jsonl"
        self.guard = Guard(self.cfg, session=self.market.session)
        self.last_decision = self.last_news = 0.0
        tz = ZoneInfo(self.cfg["timezone"])
        today = datetime.fromtimestamp(clock(), tz)
        past = today.strftime("%H:%M") >= self.cfg["daily_report"]
        self.last_report_day = today.date() if past else today.date() - timedelta(days=1)
        self.pending_urgent: list[dict] = []
        self.plans_path = state / "aitrader_plans.json"
        self.plans: list[dict] = json.loads(self.plans_path.read_text()) if self.plans_path.exists() else []
        self.universe: list[str] = []
        self.plans_checked = clock()
        self.lessons_path = state / "aitrader_lessons.json"
        self.reviews_log = logs / "aitrader_reviews.jsonl"
        self.price_hist: dict[str, list[tuple[float, float]]] = {}  # coin -> recent (time, price), 15 min
        self.move_alerted: dict[str, float] = {}
        self.pending_events: list[str] = []

    # -- conditional entries ("plans") ---------------------------------------------------
    # The AI's "wait for X, then enter" ideas, executed by code between decisions. A plan fires
    # on a touch of the trigger price or on a 1h / 4h candle closing beyond it; the entry then
    # goes through the same Guard as any other open, re-checked at the price of that moment.
    TF_MS = {"1h": 3_600_000, "4h": 14_400_000}

    def save_plans(self) -> None:
        self.plans_path.write_text(json.dumps(self.plans, ensure_ascii=False, indent=1))

    @staticmethod
    def plan_text(p: dict) -> str:
        how = {"touch": "碰到", "1h": "1h 收盤", "4h": "4h 收盤"}[p["confirm"]]
        where = "站上" if p["when"] == "above" else "跌破"
        tp = f"，止盈 {fmt(p['tp'])}" if p.get("tp") else ""
        return (f"{p['coin']}：{how}{where} {fmt(p['trigger'])} 就{'做多' if p['side'] == 'long' else '做空'}，"
                f"停損 {fmt(p['stop'])}{tp}，風險 {p['risk_pct']:g}%，有效到 {tw(p['expires'])}")

    def plans_text(self) -> str:
        return "\n".join(f"{self.plan_text(p)}（理由：{p['reason']}）" for p in self.plans) or "（沒有）"

    def add_plan(self, a: dict, universe: list[str]) -> dict:
        c = self.cfg
        coin, side = str(a.get("coin", "")).upper(), a.get("side")
        out = {"type": "plan", "coin": coin, "reason": str(a.get("reason", ""))[:300]}
        try:
            trigger, stop = float(a["trigger"]), float(a["stop"])
        except (KeyError, TypeError, ValueError):
            return {**out, "ok": False, "result": "條件單要有 trigger 和 stop"}
        if coin not in universe:
            return {**out, "ok": False, "result": f"{coin} 不在前 {c['universe']} 大"}
        if side not in ("long", "short"):
            return {**out, "ok": False, "result": "side 要是 long/short"}
        try:
            price = self.market.price(coin)
        except Exception as exc:
            return {**out, "ok": False, "result": f"拿不到價格：{exc}"}
        when = a.get("when") or ("above" if trigger > price else "below")
        confirm = a.get("confirm") or "touch"
        if when not in ("above", "below") or confirm not in ("touch", "1h", "4h"):
            return {**out, "ok": False, "result": "when 要是 above/below，confirm 要是 touch/1h/4h"}
        if (when == "above") != (trigger > price):
            return {**out, "ok": False, "result": f"現價 {fmt(price)} 已經{'在觸發價上方' if when == 'above' else '在觸發價下方'}，直接用 open"}
        dist = (trigger - stop) / trigger * (1 if side == "long" else -1)
        if not c["min_stop_pct"] / 100 <= dist <= c["max_stop_pct"] / 100:
            return {**out, "ok": False, "result": f"停損離觸發價 {dist * 100:.2f}%，不在 {c['min_stop_pct']}–{c['max_stop_pct']}%"}
        hours = min(max(float(a.get("expires_hours") or 24), 1), c["plan_max_hours"])
        try:
            tp = float(a["take_profit"]) if a.get("take_profit") else None
        except (TypeError, ValueError):
            tp = None
        plan = {"coin": coin, "side": side, "trigger": trigger, "when": when, "confirm": confirm, "stop": stop,
                "tp": tp, "risk_pct": min(max(float(a.get("risk_pct") or 1), 0.1), c["max_risk_pct"]),
                "created": self.clock(), "expires": self.clock() + hours * 3600, "reason": out["reason"]}
        replaced = any(p["coin"] == coin for p in self.plans)
        others = [p for p in self.plans if p["coin"] != coin]
        if len(others) >= c["max_plans"]:
            return {**out, "ok": False, "result": f"已有 {c['max_plans']} 張條件單，先取消舊的"}
        self.plans = others + [plan]
        self.save_plans()
        return {**out, "ok": True, "result": "更新條件單" if replaced else "掛條件單",
                "msg": f"📌 {'更新' if replaced else '掛'}條件單 {self.plan_text(plan)}\n理由：{plan['reason']}"}

    def cancel_plan(self, a: dict) -> dict:
        coin = str(a.get("coin", "")).upper()
        out = {"type": "cancel_plan", "coin": coin, "reason": str(a.get("reason", ""))[:300]}
        if not any(p["coin"] == coin for p in self.plans):
            return {**out, "ok": False, "result": "沒有這張條件單"}
        self.plans = [p for p in self.plans if p["coin"] != coin]
        self.save_plans()
        return {**out, "ok": True, "result": "取消條件單", "msg": f"🗑 取消 {coin} 條件單\n理由：{out['reason']}"}

    def plan_hit(self, p: dict, since: float) -> bool:
        sym = f"{p['coin']}/USDT:USDT"
        above = p["when"] == "above"
        if p["confirm"] == "touch":
            bars = self.ex.fetch_ohlcv(sym, "1m", since=int(max(since, p["created"]) // 60 * 60_000), limit=500)
            return any((b[2] >= p["trigger"]) if above else (b[3] <= p["trigger"]) for b in bars
                       if b[0] + 60_000 > p["created"] * 1000)
        tf = self.TF_MS[p["confirm"]]
        bars = self.ex.fetch_ohlcv(sym, p["confirm"], limit=3)
        done = [b for b in bars if b[0] + tf <= self.clock() * 1000 and b[0] + tf > p["created"] * 1000]
        return any((b[4] > p["trigger"]) if above else (b[4] < p["trigger"]) for b in done)

    def check_plans(self) -> None:
        now, since = self.clock(), self.plans_checked - 60
        self.plans_checked = now
        if not self.plans:
            return
        keep, changed = [], False
        for p in self.plans:
            if now >= p["expires"]:
                changed = True
                self._journal({"view": f"條件單過期：{self.plan_text(p)}", "actions": []}, "條件單過期", {})
                continue
            try:
                hit = self.plan_hit(p, since)
            except Exception as exc:
                log.warning("plan %s: %s", p["coin"], exc)
                hit = False
            if not hit:
                keep.append(p)
                continue
            changed = True
            a = {"type": "open", "coin": p["coin"], "side": p["side"], "stop": p["stop"], "take_profit": p["tp"],
                 "risk_pct": p["risk_pct"], "reason": f"條件單觸發。{p['reason']}"}
            universe = self.universe or [p["coin"]]
            results = [self.execute(a, universe, self.book)]
            mirror = {b.label: [self.execute(a, universe, b)] for b in self.books[1:]}
            self._journal({"view": f"條件單觸發：{self.plan_text(p)}", "actions": results, "mirror": mirror},
                          "條件單觸發", {})
            r = results[0]
            self.notify(f"⚡ 條件單觸發：{self.plan_text(p)}\n\n"
                        + (r["msg"] if r.get("ok") else f"⛔ 沒有執行：{r['result']}"))
        if changed:
            self.plans = keep
            self.save_plans()

    # -- prices / stops ---------------------------------------------------------------
    def prices(self) -> dict[str, float]:
        out = {}
        for c in {c for b in self.books for c in b.positions}:
            try:
                out[c] = self.market.price(c)
            except Exception as exc:
                log.warning("price %s: %s", c, exc)
        return out

    def check_events(self) -> None:
        """Position milestones the AI would want to act on between its hourly decisions: first time
        at +1R / +2R (stop management), or a move of event_move_r (in R) within 15 minutes. Only
        wakes the AI; what to do stays its decision."""
        if not self.cfg["event_triggers"] or not self.book.positions:
            self.price_hist.clear()
            return
        now, changed = self.clock(), False
        prices = self.prices()
        for coin, p in self.book.positions.items():
            px = prices.get(coin)
            risk = abs(p["entry"] - p.get("init_stop", p["stop"]))
            if not px or not risk:
                continue
            sgn = 1 if p["side"] == "long" else -1
            r = (px - p["entry"]) * sgn / risk
            for level in (1, 2):
                if r >= level and not p.get(f"hit_{level}R"):
                    p[f"hit_{level}R"] = changed = True
                    self.pending_events.append(f"{coin} 浮盈首次到 +{level}R（現價 {fmt(px)}，停損 {fmt(p['stop'])}，"
                                               f"止盈 {fmt(p['tp']) if p.get('tp') else '無'}）")
            hist = [(t, x) for t, x in self.price_hist.get(coin, []) if now - t <= 15 * 60] + [(now, px)]
            self.price_hist[coin] = hist
            move = (px - hist[0][1]) / risk
            if abs(move) >= self.cfg["event_move_r"] and now - self.move_alerted.get(coin, 0) >= 3600:
                self.move_alerted[coin] = now
                self.pending_events.append(f"{coin} 15 分鐘內{'上漲' if move > 0 else '下跌'} {abs(move):.1f}R"
                                           f"（{fmt(hist[0][1])} → {fmt(px)}，你的倉位是{'多' if sgn > 0 else '空'}單）")
        for coin in [c for c in self.price_hist if c not in self.book.positions]:
            del self.price_hist[coin]
        if changed:
            self.book.save()

    def check_exits(self) -> None:
        now = self.clock()
        for b in self.books:
            for row, icon in b.sync_exits(self.ex, now):
                if b is self.book:
                    self._closed(row, icon)

    def funding(self) -> None:
        """Paper books: real funding at 00/08/16 UTC, using the rate just before settlement."""
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        slot = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 8 * 8).isoformat()
        for b in self.books:
            if b.live:
                continue
            if b.last_funding is not None and slot != b.last_funding and b.positions:
                b.apply_funding(self.market.funding(), self.prices())
            b.last_funding = slot
            b.save()

    def _closed(self, row: dict, icon: str) -> None:
        eq = self.book.equity(self.prices())
        self.notify(f"{icon} 平倉 {row['coin']} {'多' if row['side'] == 'long' else '空'}（{row['why']}）\n"
                    f"{row['entry']} → {row['exit']}，損益 {row['pnl']:+.2f} U（{row['r']:+.2f}R）\n"
                    f"權益 {eq:.2f} U")

    # -- deciding ---------------------------------------------------------------
    def positions_text(self, prices: dict, book=None) -> str:
        book = book or self.book
        if not book.positions:
            return "（目前沒有倉位）"
        out = []
        for c, p in book.positions.items():
            px = prices.get(c, p["entry"])
            sgn = 1 if p["side"] == "long" else -1
            pnl = (px - p["entry"]) * p["qty"] * sgn
            r = abs(p["entry"] - p["init_stop"]) * p["qty"]
            head = "🟢" if p["side"] == "long" else "🔴"
            out.append(f"{head} {c} {'多' if p['side'] == 'long' else '空'} 數量 {fmt(p['qty'])} "
                       f"進場 {fmt(p['entry'])}（{tw(p['opened'])}）現價 {fmt(px)}")
            out.append(f"🎯 停損 {fmt(p['stop'])} 止盈 {fmt(p['tp']) if p['tp'] else '無'} "
                       f"未實現 {pnl:+.2f} U（{pnl / r if r else 0:+.2f}R）")
            out.append(f"💡 理由：{p['reason']}")
            out.append("")
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)

    @staticmethod
    def trades(book) -> list[dict]:
        if not book.trades_csv.exists():
            return []
        with book.trades_csv.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def history_text(self, n: int = 8) -> str:
        lines = []
        if self.journal.exists():
            for raw in self.journal.read_text(encoding="utf-8").splitlines()[-n:]:
                d = json.loads(raw)
                acts = "; ".join(f"{a.get('type')} {a.get('coin', '')} {a.get('result', '')}" for a in d.get("actions", []))
                lines.append(f"{d['time']} 看法：{d.get('view', '')[:200]} | 動作：{acts or '觀望'}")
        lines.append("最近平倉：" + ("; ".join(f"{t['coin']} {t['side']} {t['pnl']}U ({t['r']}R, {t['why']})"
                                         for t in self.trades(self.book)[-10:]) or "無"))
        return "\n".join(lines)

    def build_prompt(self, universe: list[str], prices: dict, trigger: str) -> str:
        b, c = self.book, self.cfg
        eq = b.equity(prices)
        small = ("\n實盤帳戶很小：名目價值低於交易所最低下單金額的單會被拒絕（多數幣約 5 U，BTC 約 100 U、ETH 約 20 U），"
                 "倉位名目約為「權益 × 風險% ÷ 停損距離%」。" if b.live else "")
        return f"""現在時間 {tw(self.clock())}（台北時間 UTC+8，下面所有時間都是台北時間）。觸發原因：{trigger}

【帳戶（{b.label}）】權益 {eq:.2f} U（起始 {b.starting_cash:g} U，目標 {b.starting_cash * c['target_multiple']:g} U），已用槓桿 {b.notional(prices) / eq if eq else 0:.2f} 倍
【硬性限制（程式強制，違反的動作會被拒絕）】每筆風險 ≤ {c['max_risk_pct']:g}% 權益；總槓桿 ≤ {c['max_leverage']:g} 倍；最多 {c['max_positions']} 個倉位、一幣一倉、不加倉；停損距離 {c['min_stop_pct']}–{c['max_stop_pct']}%；停損只能收緊；股票類 TradFi 只能在本地市場開盤中、離收盤 {c['session_buffer_minutes']} 分鐘以上開新倉（原物料看期貨交易時段）；權益 ≤ {b.floor:g} U 停止開新倉；當天（台北時間）虧損 ≥ {c['daily_loss_pct']:g}% 停止開新倉到隔天，今天目前 {-b.day_loss_pct(eq):+.1f}%。{small}

【目前倉位】
{self.positions_text(prices)}

【你掛著的條件單（程式每分鐘檢查，觸發就照風控開倉）】
{self.plans_text()}

【你最近的決策與交易】
{self.history_text()}

【你自己每週檢討寫下的教訓（參考，不是硬性規則；硬性限制只有上面那些）】
{self.lessons_text()}

【前 {len(universe)} 個 U 本位永續（加密前 {c['universe']} 大 + 成交額最大的 TradFi，依 24h 成交額）】
{self.market.digest(universe)}

【最近 48 小時新聞標題（新的在上）】
{self.news.digest()}
"""

    def decide(self, trigger: str) -> None:
        universe = self.market.universe()
        prices = self.prices()
        for b in self.books:
            if b.btc_start is None:
                b.btc_start = self.market.price("BTC")
                b.save()
        prompt = self.build_prompt(universe, prices, trigger)
        self.last_decision = self.clock()
        try:
            decision, meta = self.ai.decide(prompt)
        except Exception as exc:
            log.error("decision failed: %s", exc)
            self._journal({"view": f"決策失敗：{str(exc)[:200]}", "actions": []}, trigger, {})
            self.notify(f"⚠️ 這次決策失敗（倉位的停損、止盈照常有效）：{str(exc)[:200]}")
            return
        self.universe = universe
        actions = decision.get("actions", [])[:10]
        plan_acts = [a for a in actions if a.get("type") in ("plan", "cancel_plan")]
        actions = [a for a in actions if a not in plan_acts]
        results = [self.execute(a, universe, self.book) for a in actions]
        mirror = {b.label: [self.execute(a, universe, b) for a in actions] for b in self.books[1:]}
        results += [self.add_plan(a, universe) if a["type"] == "plan" else self.cancel_plan(a) for a in plan_acts]
        decision["actions"] = results
        self._journal({**decision, "mirror": mirror} if mirror else decision, trigger, meta)
        acted = [a for a in results if a.get("ok")]
        rejected = [a for a in results if not a.get("ok")]
        # plain paragraphs, one per coin (the prompt asks for "\n\n" between them); the actions follow
        msg = [] if acted or rejected else ["👀 觀望"]
        msg.append(str(decision.get("view", "")).strip()[:1500])
        msg += [a["msg"] for a in acted]
        msg += [f"⛔ 沒有執行：{ACTION_NAMES.get(a.get('type'), a.get('type'))} {a.get('coin', '')} — {a['result']}"
                for a in rejected]
        if decision.get("watch"):
            msg.append("等待：\n" + str(decision["watch"]).strip()[:900])
        self.notify("\n\n".join(m for m in msg if m))

    def execute(self, a: dict, universe: list[str], book) -> dict:
        kind, coin = a.get("type"), str(a.get("coin", "")).upper()
        out = {"type": kind, "coin": coin, "reason": str(a.get("reason", ""))[:300]}
        try:
            return {**out, **self._execute(kind, coin, a, universe, book, out["reason"])}
        except LiveError as exc:
            return {**out, "ok": False, "result": str(exc)}
        except Exception as exc:
            log.exception("%s %s %s failed", book.label, kind, coin)
            return {**out, "ok": False, "result": f"交易所錯誤：{str(exc)[:200]}"}

    def _execute(self, kind, coin, a, universe, book, reason) -> dict:
        try:
            price = self.market.price(coin)
        except Exception as exc:
            return {"ok": False, "result": f"拿不到價格：{exc}"}
        prices = {**self.prices(), coin: price}
        now = self.clock()
        if kind == "open":
            ok, note, n = self.guard.check_open(a, book, price, prices, universe)
            if not ok:
                return {"ok": False, "result": note}
            p = book.open(coin, n["side"], price, n["qty"], n["stop"], n["tp"], n["reason"], now)
            eq = book.equity(prices)
            loss = abs(p["entry"] - p["stop"]) * p["qty"]
            return {"ok": True, "result": f"開倉 {loss / eq * 100:.2f}%{note}",
                    "msg": f"🟢 開{'多' if n['side'] == 'long' else '空'} {coin} @ {fmt(p['entry'])}{note}\n"
                           f"停損 {fmt(p['stop'])}（打到虧 {loss:.2f} U = {loss / eq * 100:.1f}%）"
                           f"{'，止盈 ' + fmt(p['tp']) if p['tp'] else ''}\n"
                           f"倉位 {p['qty'] * p['entry']:.1f} U（{p['qty'] * p['entry'] / eq:.1f} 倍）\n理由：{n['reason']}"}
        if kind == "close":
            if coin not in book.positions:
                return {"ok": False, "result": "沒有倉位"}
            row = book.close(coin, price, "AI 平倉", now)
            return {"ok": True, "result": f"{row['pnl']:+.2f}U",
                    "msg": f"⚪ 平倉 {coin}：{row['entry']} → {row['exit']}，{row['pnl']:+.2f} U（{row['r']:+.2f}R）\n"
                           f"理由：{reason}"}
        if kind == "move_stop":
            ok, note, n = self.guard.check_stop(a, book, price)
            if not ok:
                return {"ok": False, "result": note}
            old = book.positions[coin]["stop"]
            book.move_stop(coin, n["stop"], now)
            return {"ok": True, "result": f"{fmt(old)}→{fmt(n['stop'])}",
                    "msg": f"🔧 {coin} 停損 {fmt(old)} → {fmt(n['stop'])}\n理由：{reason}"}
        if kind == "take_profit":
            p = book.positions.get(coin)
            if not p:
                return {"ok": False, "result": "沒有倉位"}
            try:
                tp = float(a["take_profit"]) if a.get("take_profit") else None
            except (TypeError, ValueError):
                return {"ok": False, "result": "止盈價格不對"}
            if tp is not None and (tp - price) * (1 if p["side"] == "long" else -1) <= 0:
                return {"ok": False, "result": "止盈在錯的一邊"}
            book.set_tp(coin, tp)
            return {"ok": True, "result": f"止盈 {fmt(tp) if tp else '取消'}",
                    "msg": f"🎯 {coin} 止盈改為 {fmt(tp) if tp else '無'}\n理由：{reason}"}
        return {"ok": False, "result": f"不認得的動作 {kind}"}

    def _journal(self, decision: dict, trigger: str, meta: dict) -> None:
        row = {"time": tw(self.clock()), "trigger": trigger, "view": decision.get("view", ""),
               "watch": decision.get("watch", ""), "actions": decision.get("actions", []),
               "cost_usd": meta.get("total_cost_usd"), "turns": meta.get("num_turns")}
        if decision.get("mirror"):
            row["mirror"] = decision["mirror"]
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- reporting ---------------------------------------------------------------
    def report(self) -> str:
        prices = self.prices()
        try:
            btc_now = self.market.price("BTC")
        except Exception:
            btc_now = None
        days = (self.clock() - self.book.started) / 86400
        out = [f"📊 AI 交易員日報（第 {days:.1f} 天）"]
        for b in self.books:
            eq = b.equity(prices)
            trades = self.trades(b)
            wins = sum(1 for t in trades if float(t["pnl"]) > 0)
            btc = (btc_now / b.btc_start - 1) * 100 if btc_now and b.btc_start else 0.0
            out.append(f"【{b.label}】權益 {eq:.2f} U（{(eq / b.starting_cash - 1) * 100:+.1f}%），同期 BTC {btc:+.1f}%，"
                       f"已平倉 {len(trades)} 筆、賺 {wins} 筆")
        out.append(f"\n{self.book.label}倉位：\n{self.positions_text(prices)}")
        return "\n".join(out)

    def maybe_report(self) -> None:
        tz = ZoneInfo(self.cfg["timezone"])
        now = datetime.fromtimestamp(self.clock(), tz)
        if not self.cfg["daily_report"] or now.strftime("%H:%M") < self.cfg["daily_report"]:
            return
        if self.last_report_day != now.date():
            self.last_report_day = now.date()
            self.notify(self.report())

    # -- weekly self-review --------------------------------------------------------
    # Once a week the AI gets every trade it closed in the last 7 days with what the market did
    # during and after it, and rewrites a short list of lessons. The lessons are shown in every
    # later decision prompt; they never change the hard limits (Guard).
    def lessons(self) -> dict:
        try:
            return json.loads(self.lessons_path.read_text(encoding="utf-8"))
        except Exception:
            return {"lessons": [], "updated": None}

    def lessons_text(self) -> str:
        d = self.lessons()
        if not d.get("lessons"):
            return "（還沒有，第一次每週檢討後才會有）"
        return f"（{d.get('updated')} 檢討）\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(d["lessons"], 1))

    def excursions(self, t: dict) -> dict:
        """What price did while the trade was open (best / worst, in R) and in the 24 h after it closed."""
        sgn = 1 if t["side"] == "long" else -1
        entry, exit_, qty = float(t["entry"]), float(t["exit"]), float(t["qty"])
        r, pnl = float(t["r"] or 0), float(t["pnl"] or 0)
        risk = abs(pnl / r) / qty if r and qty else 0.0  # price distance of 1R (initial stop)
        ms = lambda s: int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TW).timestamp() * 1000)
        opened, closed = ms(t["opened"]), ms(t["closed"])
        try:
            bars = self.ex.fetch_ohlcv(f"{t['coin']}/USDT:USDT", "15m", since=opened, limit=1000)
        except Exception as exc:
            log.warning("review klines %s: %s", t["coin"], exc)
            return {}
        during = [b for b in bars if opened <= b[0] <= closed]
        after = [b for b in bars if closed < b[0] <= closed + 86_400_000]
        if not risk or not during:
            return {}
        fav = lambda b: (b[2] - entry) if sgn > 0 else (entry - b[3])  # high for a long, low for a short
        adv = lambda b: (entry - b[3]) if sgn > 0 else (b[2] - entry)
        out = {"best_R": round(max(fav(b) for b in during) / risk, 2),
               "worst_R": round(-max(adv(b) for b in during) / risk, 2)}
        if after:
            beyond = max((b[2] - exit_) if sgn > 0 else (exit_ - b[3]) for b in after)
            against = max((exit_ - b[3]) if sgn > 0 else (b[2] - exit_) for b in after)
            out["after_24h_further_R"] = round(beyond / risk, 2)
            out["after_24h_against_R"] = round(against / risk, 2)
        return out

    def review_prompt(self, trades: list[dict]) -> str:
        c = self.cfg
        rows = []
        for t in trades:
            ex = self.excursions(t)
            rows.append(f"- {t['coin']} {t['side']} | 開 {t['opened']} → 平 {t['closed']} | 進 {t['entry']} 出 {t['exit']} | "
                        f"{t['pnl']} U，{t['r']}R，出場：{t['why']} | 持倉中最好 {ex.get('best_R', '?')}R、最差 {ex.get('worst_R', '?')}R | "
                        f"出場後 24h 再往有利方向 {ex.get('after_24h_further_R', '?')}R、往不利方向 {ex.get('after_24h_against_R', '?')}R\n"
                        f"  開倉理由：{t['reason']}")
        all_trades = self.trades(self.book)
        wins = [float(t["r"]) for t in all_trades if float(t["pnl"]) > 0]
        losses = [float(t["r"]) for t in all_trades if float(t["pnl"]) <= 0]
        return f"""這不是交易決策，是【每週檢討】。現在時間 {tw(self.clock())}（台北時間）。

【這 7 天平倉的 {len(trades)} 筆交易（{self.book.label}）】
{chr(10).join(rows) or '（這週沒有平倉）'}

【從開始到現在】共 {len(all_trades)} 筆，賺 {len(wins)} 筆（平均 {sum(wins) / len(wins) if wins else 0:+.2f}R），賠 {len(losses)} 筆（平均 {sum(losses) / len(losses) if losses else 0:+.2f}R）。

【你目前的教訓】
{self.lessons_text()}

請做三件事：
1. 找出這週做對和做錯的模式：哪種進場（突破追、回測接、條件單、試單）、哪種出場（停損、收緊停損、AI 平倉、止盈）賺或賠；出場後價格是否照原計畫走（太早出場）；停損是否被正常回檔掃掉。數字以上面的資料為準，可以用 market.py 查更多 K 線，但不要編造。
2. 重寫教訓清單：最多 {c['lessons_max']} 條，每條一句具體可執行的做法，後面括號寫證據和樣本數（例如「突破追單等 1h 收盤確認再進（這週 5 筆 4 輸，確認後進場的 3 筆 2 贏）」）。樣本少於 3 筆的寫「待觀察」。舊教訓仍成立就保留，被新數據推翻就刪掉或改寫。教訓不能要求違反或放寬硬性限制。
3. 用幾句話寫給使用者看的檢討摘要。

最後一定要附上 ```json 區塊：
{{"review": "給使用者看的摘要，繁體中文，段落之間空一行", "lessons": ["教訓 1（證據）", "教訓 2（證據）"]}}
"""

    def weekly_review(self) -> None:
        now = self.clock()
        week_ago = tw(now - 7 * 86400)
        trades = [t for t in self.trades(self.book) if t["closed"] >= week_ago]
        try:
            d, meta = self.ai.review(self.review_prompt(trades))
        except Exception as exc:
            log.error("weekly review failed: %s", exc)
            self.notify(f"⚠️ 這週的 AI 自我檢討失敗：{str(exc)[:200]}（下次決策照舊）")
            return
        lessons = [str(s).strip()[:300] for s in (d.get("lessons") or []) if str(s).strip()][: self.cfg["lessons_max"]]
        if lessons:
            self.lessons_path.write_text(json.dumps({"lessons": lessons, "updated": tw(now)}, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
        with self.reviews_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": tw(now), "trades": len(trades), "review": d.get("review", ""), "lessons": lessons,
                                "cost_usd": meta.get("total_cost_usd")}, ensure_ascii=False) + "\n")
        body = str(d.get("review", "")).strip()[:2000]
        listed = "\n".join(f"{i}. {s}" for i, s in enumerate(lessons, 1)) or "（沒有更新教訓，沿用舊的）"
        self.notify(f"📝 AI 每週檢討（{len(trades)} 筆）\n\n{body}\n\n教訓：\n{listed}")

    def maybe_review(self) -> None:
        spec = self.cfg.get("weekly_review") or ""
        if not spec:
            return
        day, at = spec.split()
        tz = ZoneInfo(self.cfg["timezone"])
        now = datetime.fromtimestamp(self.clock(), tz)
        weekday = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(day[:3].lower())
        if now.weekday() != weekday or now.strftime("%H:%M") < at:
            return
        today = now.strftime("%Y-%m-%d")
        if self.lessons().get("reviewed_day") == today or getattr(self, "_reviewed_day", None) == today:
            return
        self._reviewed_day = today
        self.weekly_review()
        d = self.lessons()
        d["reviewed_day"] = today
        self.lessons_path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")

    # -- main loop ---------------------------------------------------------------
    def step(self) -> None:
        now = self.clock()
        self.check_exits()
        self.funding()
        for b in self.books:
            b.mark_day(now, lambda b=b: b.equity(self.prices()))
        self.check_plans()
        if now - self.last_news >= self.cfg["news_minutes"] * 60:
            first = self.last_news == 0
            new = self.news.poll()
            self.last_news = now
            if not first:
                self.pending_urgent += self.news.urgent(new)
        self.check_events()
        trigger = None
        if now - self.last_decision >= self.cfg["decide_minutes"] * 60:
            trigger = "定時檢查"
        elif self.pending_urgent and now - self.last_decision >= self.cfg["urgent_gap_minutes"] * 60:
            trigger = "突發新聞：" + " / ".join(i["title"] for i in self.pending_urgent[:5])
        elif self.pending_events and now - self.last_decision >= self.cfg["event_gap_minutes"] * 60:
            trigger = "持倉事件"
        if trigger:
            if self.pending_events:
                events = " / ".join(self.pending_events[:5])
                trigger = f"持倉事件：{events}" if trigger == "持倉事件" else f"{trigger}；持倉事件：{events}"
            self.pending_urgent, self.pending_events = [], []
            self.decide(trigger)
        self.maybe_report()
        self.maybe_review()

    def run(self) -> None:
        prices = self.prices()
        self.notify("🤖 AI 交易員啟動：" + "，".join(f"{b.label} {b.equity(prices):.2f} U" for b in self.books))
        while True:
            try:
                self.step()
            except Exception as exc:
                log.exception("step failed: %s", exc)
            time.sleep(60)


def status_text(cfg: dict, price, live_equity=None, now: float | None = None) -> str:
    """Telegram /ai: the AI sub-account's positions, stops, plans and results, from the AI trader's
    own state files (it syncs them with the exchange every minute)."""
    c = settings(cfg)
    state, logs = Path(cfg.get("state_dir", "state")), Path(cfg.get("log_dir", "logs"))
    path = state / "aitrader_live_book.json"
    if not path.exists():
        return "🤖 AI 子帳戶還沒有資料（AI 交易員沒有用 --live 跑過）"
    b = json.loads(path.read_text())
    lines = [f"🤖 AI 子帳戶 {datetime.fromtimestamp(now or time.time(), TW):%m/%d %H:%M}"]
    if live_equity is not None:
        start = c["live_cash"]
        day = f"｜今天 {(live_equity / b['day_start'] - 1) * 100:+.1f}%" if b.get("day_start") else ""
        lines.append(f"💰 權益 {live_equity:.2f} U（起始 {start:g} U，{(live_equity / start - 1) * 100:+.1f}%）{day}")
    pos = b.get("positions") or {}
    if pos:
        lines.append(f"📌 持倉 {len(pos)} 筆")
        for coin, p in pos.items():
            sgn = 1 if p["side"] == "long" else -1
            try:
                px = float(price(coin))
                upnl = f" → {fmt(px)}  {(px - p['entry']) * p['qty'] * sgn:+.2f} U"
            except Exception:
                upnl = ""
            at_stop = (p["stop"] - p["entry"]) * p["qty"] * sgn
            lines.append(f"{coin} {'多' if sgn > 0 else '空'} {fmt(p['qty'])} @ {fmt(p['entry'])}{upnl}")
            lines.append(f"    停損 {fmt(p['stop'])}（打到 {at_stop:+.2f} U）｜止盈 {fmt(p['tp']) if p.get('tp') else '—'}")
    else:
        lines.append("📌 目前沒有持倉")
    plans_path = state / "aitrader_plans.json"
    plans = json.loads(plans_path.read_text()) if plans_path.exists() else []
    if plans:
        lines.append(f"📎 條件單 {len(plans)} 張")
        lines += [AITrader.plan_text(p) for p in plans]
    csv_path = logs / "aitrader_live_trades.csv"
    trades = list(csv.DictReader(csv_path.open(encoding="utf-8"))) if csv_path.exists() else []
    if trades:
        total = sum(float(t["pnl"]) for t in trades)
        wins = sum(1 for t in trades if float(t["pnl"]) > 0)
        lines.append(f"🧾 已平倉 {len(trades)} 筆（賺 {wins} 筆）合計 {total:+.2f} U")
    journal = logs / "aitrader_decisions.jsonl"
    if journal.exists():
        last = json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
        view = str(last.get("view", "")).split("\n\n")
        lines.append(f"\n🕐 最後一次決策 {last['time']}")
        lines.append("\n\n".join(view[:3])[:700])
    return "\n".join(lines)


def live_test(book: LiveBook, coin: str) -> int:
    """Smallest real round trip: open, place stop + take-profit on the exchange, close."""
    b = book.broker(coin)
    price = float(book.ex.fetch_ticker(b.symbol)["last"])
    m = book.ex.market(b.symbol)
    min_cost = ((m.get("limits") or {}).get("cost") or {}).get("min") or 5
    qty = float(book.ex.amount_to_precision(b.symbol, min_cost * 1.3 / price))
    p = book.open(coin, "long", price, qty, price * 0.95, price * 1.05, "連線測試", time.time())
    print(f"opened {coin} qty={p['qty']} entry={p['entry']} stop_id={p['stop_id']} tp_id={p['tp_id']} lev={p['leverage']}")
    orders = book.ex.fetch_open_orders(b.symbol, params={"trigger": True})
    print("open trigger orders:", [(o.get("type"), o.get("triggerPrice") or o.get("stopPrice")) for o in orders])
    row = book.close(coin, price, "連線測試", time.time())
    print("closed:", row)
    left = book.ex.fetch_open_orders(b.symbol, params={"trigger": True})
    print("trigger orders left:", len(left), "| positions:", book.exchange_positions())
    return 0


def main(cfg: dict, repo_dir: str, once: bool = False, live: bool = False, check: bool = False,
         test_coin: str | None = None) -> int:
    import os

    from .data import make_exchange
    from .notify import Telegram

    c = settings(cfg)
    state, logs = Path(cfg.get("state_dir", "state")), Path(cfg.get("log_dir", "logs"))
    state.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    paper = Book(state / "aitrader_book.json", logs / "aitrader_trades.csv", c, c["paper_cash"])
    books, ex = [paper], None
    if live or check or test_coin:
        key, secret = os.getenv("AITRADER_API_KEY"), os.getenv("AITRADER_API_SECRET")
        if not key or not secret:
            print("AITRADER_API_KEY / AITRADER_API_SECRET missing (/etc/cryptobot-aitrader/live.env)")
            return 1
        ex = make_exchange(cfg, key, secret)
        lb = LiveBook(state / "aitrader_live_book.json", logs / "aitrader_live_trades.csv", c, c["live_cash"], ex)
        problem = lb.check_account()
        print(f"live account: equity {lb.equity({}):.2f} U, positions {lb.exchange_positions()}")
        if problem:
            print("❌", problem)
            return 1
        if check:
            print("✅ account OK")
            return 0
        if test_coin:
            return live_test(lb, test_coin.upper())
        books = [lb, paper]
    token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    claude = Path(AI_ROOT) / "home" / ".local" / "bin" / "claude"
    if not token or not claude.exists():
        print("CLAUDE_CODE_OAUTH_TOKEN or the claude binary is missing")
        return 1
    ai = TraderAI(cfg, repo_dir, str(claude), token, c["model"], int(c["max_turns"]))
    ai.install()
    tg_token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    prefix = "[AI真倉] " if len(books) > 1 else "[AI模擬] "
    notify = Telegram(tg_token, chat, prefix=prefix) if tg_token and chat else None
    trader = AITrader(cfg, ex or make_exchange(cfg), ai, notify, books=books)
    if once:
        if trader.book.live:
            print("--once is paper only")
            return 1
        trader.news.poll()
        trader.check_exits()
        trader.decide("手動測試")
        print(trader.journal.read_text(encoding="utf-8").splitlines()[-1])
        return 0
    trader.run()
    return 0
