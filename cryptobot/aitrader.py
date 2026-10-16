"""AI discretionary trader (paper): Claude reads the top-50 market and the news and decides.

    python3 -m cryptobot ai-trader            # loop forever (systemd cryptobot-aitrader)
    python3 -m cryptobot ai-trader --once     # one decision now, then exit (testing)

The AI only *proposes* actions as JSON; every action passes hard limits in code (Guard) that it
cannot change: a stop on every trade, max risk per trade, max account leverage, max positions,
stops may only be tightened, and no new trades below the equity floor. Fills are simulated on
live Binance prices (taker fee + slippage, real funding at 00/08/16 UTC). No API key is used.
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
    "starting_cash": 200.0,
    "model": "opus",
    "max_turns": 25,
    "decide_minutes": 60,  # regular decision every hour
    "urgent_gap_minutes": 30,  # breaking news may trigger an extra decision, at most this often
    "news_minutes": 5,
    "universe": 50,
    "max_positions": 4,
    "max_risk_pct": 5.0,  # loss at the stop, % of equity, per trade
    "max_leverage": 5.0,  # total open notional / equity
    "floor": 100.0,  # no new positions at or below this equity
    "min_stop_pct": 0.3,
    "max_stop_pct": 25.0,
    "taker_fee": 0.0005,
    "slippage": 0.0003,
    "daily_report": "09:00",
    "timezone": "Asia/Taipei",
}
STABLES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "USD1", "XUSD", "EUR"}
FEEDS = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss",
    "Cointelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "The Block": "https://www.theblock.co/rss.xml",
}
BINANCE_NEWS = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
                "?type=1&pageNo=1&pageSize=10")
URGENT = re.compile(r"hack|exploit|drain|stolen|delist|liquidat|crash|plunge|sec\b|etf|"
                    r"fed\b|fomc|rate (cut|hike)|cpi|tariff|bankrupt|halt|outage|depeg|lawsuit|ban\b|"
                    r"approv|will list|launchpool|emergency", re.I)
UA = {"User-Agent": "Mozilla/5.0 (cryptobot ai-trader)"}


def _get(url: str, timeout: int = 15) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


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
        return "\n".join(f"{utc(i['ts'])} [{i['src']}] {i['title']}" for i in self.items[:limit])

    @staticmethod
    def urgent(items: list[dict]) -> list[dict]:
        return [i for i in items if URGENT.search(i["title"])]


# -- market -----------------------------------------------------------------------
def ema(values: list[float], n: int) -> float:
    k, e = 2 / (n + 1), values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


class Market:
    def __init__(self, ex, size: int = 50):
        self.ex, self.size = ex, size
        self.tickers: dict = {}

    def universe(self) -> list[str]:
        """Top crypto USDT perpetuals by 24 h quote volume (no stablecoins, no stock tokens)."""
        self.tickers = self.ex.fetch_tickers()
        rows = []
        for sym, t in self.tickers.items():
            m = self.ex.markets.get(sym) or {}
            info = m.get("info") or {}
            if not (m.get("swap") and m.get("quote") == "USDT" and m.get("active", True)):
                continue
            if info.get("contractType", "PERPETUAL") != "PERPETUAL":
                continue
            if info.get("underlyingType", "COIN") != "COIN" or m.get("base") in STABLES:
                continue
            rows.append((float(t.get("quoteVolume") or 0), m["base"]))
        return [b for _, b in sorted(rows, reverse=True)[: self.size]]

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

    def digest(self, coins: list[str]) -> str:
        fund = self.funding()
        lines = ["幣種 | 價格 | 24h% | 7d% | 4h EMA50 位置 | 距20根4h高點% | 距20根4h低點% | ATR14(4h)% | 資金費率%/8h | 24h成交額M"]
        for c in coins:
            sym = f"{c}/USDT:USDT"
            try:
                k = self.ex.fetch_ohlcv(sym, "4h", limit=60)
            except Exception as exc:
                log.warning("klines %s: %s", c, exc)
                continue
            if len(k) < 50:
                continue
            closes = [r[4] for r in k]
            last = closes[-1]
            hi = max(r[2] for r in k[-20:])
            lo = min(r[3] for r in k[-20:])
            trs = [max(k[i][2] - k[i][3], abs(k[i][2] - k[i - 1][4]), abs(k[i][3] - k[i - 1][4]))
                   for i in range(len(k) - 14, len(k))]
            e50 = ema(closes, 50)
            t = self.tickers.get(sym, {})
            lines.append(
                f"{c} | {fmt(last)} | {float(t.get('percentage') or 0):+.1f} | {(last / closes[-43] - 1) * 100:+.1f} | "
                f"{'上' if last > e50 else '下'}{abs(last / e50 - 1) * 100:.1f}% | {(last / hi - 1) * 100:+.1f} | "
                f"{(last / lo - 1) * 100:+.1f} | {sum(trs) / 14 / last * 100:.1f} | {fund.get(c, 0) * 100:+.4f} | "
                f"{float(t.get('quoteVolume') or 0) / 1e6:.0f}")
        return "\n".join(lines)


# -- paper book + hard limits -----------------------------------------------------------
class Book:
    """Simulated USDT-perpetual account. State survives restarts (JSON)."""

    def __init__(self, path: Path, trades_csv: Path, cfg: dict):
        self.path, self.trades_csv, self.cfg = path, trades_csv, cfg
        if path.exists():
            s = json.loads(path.read_text())
        else:
            s = {"cash": cfg["starting_cash"], "positions": {}, "started": time.time(), "last_check": time.time(),
                 "last_funding": None}
        self.cash: float = s["cash"]
        self.positions: dict[str, dict] = s["positions"]
        self.started: float = s["started"]
        self.last_check: float = s["last_check"]
        self.last_funding = s.get("last_funding")
        self.btc_start = s.get("btc_start")

    def save(self) -> None:
        self.path.write_text(json.dumps({"cash": self.cash, "positions": self.positions, "started": self.started,
                                         "last_check": self.last_check, "last_funding": self.last_funding,
                                         "btc_start": self.btc_start}, indent=1))

    def upnl(self, prices: dict[str, float]) -> float:
        return sum(p["qty"] * (prices.get(c, p["entry"]) - p["entry"]) * (1 if p["side"] == "long" else -1)
                   for c, p in self.positions.items())

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.upnl(prices)

    def notional(self, prices: dict[str, float]) -> float:
        return sum(p["qty"] * prices.get(c, p["entry"]) for c, p in self.positions.items())

    def open(self, coin: str, side: str, price: float, qty: float, stop: float, tp: float | None,
             reason: str, now: float) -> dict:
        fill = price * (1 + self.cfg["slippage"] * (1 if side == "long" else -1))
        fee = fill * qty * self.cfg["taker_fee"]
        self.cash -= fee
        self.positions[coin] = {"side": side, "qty": qty, "entry": fill, "stop": stop, "tp": tp, "opened": now,
                                "reason": reason, "fees": fee, "funding": 0.0, "init_stop": stop}
        self.save()
        return self.positions[coin]

    def close(self, coin: str, price: float, why: str, now: float, slip: bool = True) -> dict:
        p = self.positions.pop(coin)
        sgn = 1 if p["side"] == "long" else -1
        fill = price * (1 - self.cfg["slippage"] * sgn) if slip else price
        fee = fill * p["qty"] * self.cfg["taker_fee"]
        gross = (fill - p["entry"]) * p["qty"] * sgn
        self.cash += gross - fee
        risk = abs(p["entry"] - p["init_stop"]) * p["qty"]
        net = gross - fee - p["fees"] + p["funding"]
        row = {"coin": coin, "side": p["side"], "opened": utc(p["opened"]), "closed": utc(now), "entry": fmt(p["entry"]),
               "exit": fmt(fill), "qty": fmt(p["qty"]), "pnl": round(net, 2), "r": round(net / risk, 2) if risk else 0,
               "why": why, "reason": p["reason"]}
        new = not self.trades_csv.exists()
        with self.trades_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
        self.save()
        return row

    def apply_funding(self, rates: dict[str, float], prices: dict[str, float]) -> None:
        for c, p in self.positions.items():
            amt = p["qty"] * prices.get(c, p["entry"]) * rates.get(c, 0) * (-1 if p["side"] == "long" else 1)
            p["funding"] += amt
            self.cash += amt
        self.save()


class Guard:
    """Hard limits the AI cannot override. Returns (ok, message, normalised action)."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def check_open(self, a: dict, book: Book, price: float, prices: dict, universe: list[str]):
        c = self.cfg
        coin, side = str(a.get("coin", "")).upper(), a.get("side")
        if coin not in universe:
            return False, f"{coin} 不在前 {c['universe']} 大", None
        if coin in book.positions:
            return False, f"{coin} 已有倉位（不加倉）", None
        if len(book.positions) >= c["max_positions"]:
            return False, f"已達 {c['max_positions']} 個倉位上限", None
        if side not in ("long", "short"):
            return False, "side 要是 long/short", None
        eq = book.equity(prices)
        if eq <= c["floor"]:
            return False, f"權益 {eq:.1f} ≤ 底線 {c['floor']:g}，停止開新倉", None
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
class AITrader:
    def __init__(self, cfg: dict, ex, ai, notify=None, clock=time.time, fetch=_get):
        self.cfg = {**DEFAULTS, **(cfg.get("aitrader") or {})}
        state, logs = Path(cfg.get("state_dir", "state")), Path(cfg.get("log_dir", "logs"))
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        self.ex, self.ai, self.clock = ex, ai, clock
        self.notify = notify or (lambda text: None)
        self.market = Market(ex, self.cfg["universe"])
        self.book = Book(state / "aitrader_book.json", logs / "aitrader_trades.csv", self.cfg)
        self.news = News(state / "aitrader_news.json", fetch=fetch, clock=clock)
        self.journal = logs / "aitrader_decisions.jsonl"
        self.guard = Guard(self.cfg)
        self.last_decision = self.last_news = 0.0
        tz = ZoneInfo(self.cfg["timezone"])
        today = datetime.fromtimestamp(clock(), tz)
        past = today.strftime("%H:%M") >= self.cfg["daily_report"]
        self.last_report_day = today.date() if past else today.date() - timedelta(days=1)
        self.pending_urgent: list[dict] = []

    # -- prices / stops ---------------------------------------------------------------
    def prices(self) -> dict[str, float]:
        out = {}
        for c in self.book.positions:
            try:
                out[c] = self.market.price(c)
            except Exception as exc:
                log.warning("price %s: %s", c, exc)
        return out

    def check_exits(self) -> None:
        """Stops / take-profits hit by any 1m candle since the last check."""
        now = self.clock()
        for coin, p in list(self.book.positions.items()):
            # only whole minutes after the entry / last stop change: earlier prices never applied
            start = max(self.book.last_check - 60, p.get("since", p["opened"]))
            first = int(-(-p.get("since", p["opened"]) // 60) * 60_000)
            try:
                bars = self.ex.fetch_ohlcv(f"{coin}/USDT:USDT", "1m", since=int(start // 60 * 60_000), limit=500)
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
                    self._closed(self.book.close(coin, o if gap else p["stop"], "停損", now), "🛑")
                    break
                if tp_hit:
                    self._closed(self.book.close(coin, p["tp"], "止盈", now, slip=False), "🎯")
                    break
        self.book.last_check = now
        self.book.save()

    def funding(self) -> None:
        """Real funding at 00/08/16 UTC, using the rate just before settlement."""
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        slot = now.replace(minute=0, second=0, microsecond=0, hour=now.hour // 8 * 8).isoformat()
        if self.book.last_funding is None:
            self.book.last_funding = slot
        if slot != self.book.last_funding and self.book.positions:
            self.book.apply_funding(self.market.funding(), self.prices())
        self.book.last_funding = slot
        self.book.save()

    def _closed(self, row: dict, icon: str) -> None:
        eq = self.book.equity(self.prices())
        self.notify(f"{icon} 平倉 {row['coin']} {'多' if row['side'] == 'long' else '空'}（{row['why']}）\n"
                    f"{row['entry']} → {row['exit']}，損益 {row['pnl']:+.2f} U（{row['r']:+.2f}R）\n"
                    f"權益 {eq:.2f} U")

    # -- deciding ---------------------------------------------------------------
    def positions_text(self, prices: dict) -> str:
        if not self.book.positions:
            return "（目前沒有倉位）"
        out = []
        for c, p in self.book.positions.items():
            px = prices.get(c, p["entry"])
            sgn = 1 if p["side"] == "long" else -1
            pnl = (px - p["entry"]) * p["qty"] * sgn
            r = abs(p["entry"] - p["init_stop"]) * p["qty"]
            out.append(f"{c} {p['side']} 數量 {fmt(p['qty'])} 進場 {fmt(p['entry'])} 現價 {fmt(px)} 停損 {fmt(p['stop'])} "
                       f"止盈 {fmt(p['tp']) if p['tp'] else '無'} 未實現 {pnl:+.2f} U（{pnl / r if r else 0:+.2f}R） "
                       f"開倉 {utc(p['opened'])} 理由：{p['reason']}")
        return "\n".join(out)

    def history_text(self, n: int = 8) -> str:
        lines = []
        if self.journal.exists():
            for raw in self.journal.read_text(encoding="utf-8").splitlines()[-n:]:
                d = json.loads(raw)
                acts = "; ".join(f"{a.get('type')} {a.get('coin', '')} {a.get('result', '')}" for a in d.get("actions", []))
                lines.append(f"{d['time']} 看法：{d.get('view', '')[:200]} | 動作：{acts or '觀望'}")
        trades = []
        if self.book.trades_csv.exists():
            with self.book.trades_csv.open(encoding="utf-8") as f:
                trades = list(csv.DictReader(f))[-10:]
        lines.append("最近平倉：" + ("; ".join(f"{t['coin']} {t['side']} {t['pnl']}U ({t['r']}R, {t['why']})"
                                         for t in trades) or "無"))
        return "\n".join(lines) or "（第一次決策）"

    def build_prompt(self, universe: list[str], prices: dict, trigger: str) -> str:
        eq = self.book.equity(prices)
        c = self.cfg
        return f"""現在時間 {utc(self.clock())} UTC。觸發原因：{trigger}

【帳戶（模擬）】權益 {eq:.2f} U（起始 {c['starting_cash']:g} U，目標 1000 U），已用槓桿 {self.book.notional(prices) / eq if eq else 0:.2f} 倍
【硬性限制（程式強制，違反的動作會被拒絕）】每筆風險 ≤ {c['max_risk_pct']:g}% 權益；總槓桿 ≤ {c['max_leverage']:g} 倍；最多 {c['max_positions']} 個倉位、一幣一倉、不加倉；停損距離 {c['min_stop_pct']}–{c['max_stop_pct']}%；停損只能收緊；權益 ≤ {c['floor']:g} U 停止開新倉。

【目前倉位】
{self.positions_text(prices)}

【你最近的決策與交易】
{self.history_text()}

【前 {len(universe)} 大 U 本位永續（依 24h 成交額）】
{self.market.digest(universe)}

【最近 48 小時新聞標題（新的在上）】
{self.news.digest()}
"""

    def decide(self, trigger: str) -> None:
        universe = self.market.universe()
        prices = self.prices()
        if self.book.btc_start is None:
            self.book.btc_start = self.market.price("BTC")
            self.book.save()
        prompt = self.build_prompt(universe, prices, trigger)
        self.last_decision = self.clock()
        try:
            decision, meta = self.ai.decide(prompt)
        except Exception as exc:
            log.error("decision failed: %s", exc)
            self._journal({"view": f"決策失敗：{str(exc)[:200]}", "actions": []}, trigger, {})
            return
        results = [self.execute(a, universe) for a in decision.get("actions", [])[:8]]
        decision["actions"] = results
        self._journal(decision, trigger, meta)
        acted = [a for a in results if a.get("ok")]
        rejected = [a for a in results if not a.get("ok")]
        if acted or rejected:
            msg = [f"🧠 {decision.get('view', '')[:400]}"]
            msg += [a["msg"] for a in acted]
            msg += [f"⛔ 被風控擋下：{a.get('type')} {a.get('coin', '')} — {a['result']}" for a in rejected]
            self.notify("\n\n".join(msg))

    def execute(self, a: dict, universe: list[str]) -> dict:
        kind, coin = a.get("type"), str(a.get("coin", "")).upper()
        out = {"type": kind, "coin": coin, "reason": str(a.get("reason", ""))[:300]}
        try:
            price = self.market.price(coin)
        except Exception as exc:
            return {**out, "ok": False, "result": f"拿不到價格：{exc}"}
        prices = {**self.prices(), coin: price}
        now = self.clock()
        if kind == "open":
            ok, note, n = self.guard.check_open(a, self.book, price, prices, universe)
            if not ok:
                return {**out, "ok": False, "result": note}
            p = self.book.open(coin, n["side"], price, n["qty"], n["stop"], n["tp"], n["reason"], now)
            eq = self.book.equity(prices)
            loss = abs(p["entry"] - p["stop"]) * p["qty"]
            return {**out, "ok": True, "result": f"開倉 {n['risk_pct']:.2f}%{note}",
                    "msg": f"🟢 開{'多' if n['side'] == 'long' else '空'} {coin} @ {fmt(p['entry'])}{note}\n"
                           f"停損 {fmt(n['stop'])}（打到虧 {loss:.2f} U = {loss / eq * 100:.1f}%）"
                           f"{'，止盈 ' + fmt(n['tp']) if n['tp'] else ''}\n"
                           f"倉位 {p['qty'] * p['entry']:.1f} U（{p['qty'] * p['entry'] / eq:.1f} 倍）\n理由：{n['reason']}"}
        if kind == "close":
            if coin not in self.book.positions:
                return {**out, "ok": False, "result": "沒有倉位"}
            row = self.book.close(coin, price, "AI 平倉", now)
            return {**out, "ok": True, "result": f"{row['pnl']:+.2f}U",
                    "msg": f"⚪ 平倉 {coin}：{row['entry']} → {row['exit']}，{row['pnl']:+.2f} U（{row['r']:+.2f}R）\n"
                           f"理由：{out['reason']}"}
        if kind == "move_stop":
            ok, note, n = self.guard.check_stop(a, self.book, price)
            if not ok:
                return {**out, "ok": False, "result": note}
            old = self.book.positions[coin]["stop"]
            self.book.positions[coin].update(stop=n["stop"], since=now)
            self.book.save()
            return {**out, "ok": True, "result": f"{fmt(old)}→{fmt(n['stop'])}",
                    "msg": f"🔧 {coin} 停損 {fmt(old)} → {fmt(n['stop'])}\n理由：{out['reason']}"}
        if kind == "take_profit":
            p = self.book.positions.get(coin)
            if not p:
                return {**out, "ok": False, "result": "沒有倉位"}
            try:
                tp = float(a["take_profit"]) if a.get("take_profit") else None
            except (TypeError, ValueError):
                return {**out, "ok": False, "result": "止盈價格不對"}
            if tp is not None and (tp - price) * (1 if p["side"] == "long" else -1) <= 0:
                return {**out, "ok": False, "result": "止盈在錯的一邊"}
            p["tp"] = tp
            self.book.save()
            return {**out, "ok": True, "result": f"止盈 {fmt(tp) if tp else '取消'}",
                    "msg": f"🎯 {coin} 止盈改為 {fmt(tp) if tp else '無'}\n理由：{out['reason']}"}
        return {**out, "ok": False, "result": f"不認得的動作 {kind}"}

    def _journal(self, decision: dict, trigger: str, meta: dict) -> None:
        row = {"time": utc(self.clock()), "trigger": trigger, "view": decision.get("view", ""),
               "watch": decision.get("watch", ""), "actions": decision.get("actions", []),
               "cost_usd": meta.get("total_cost_usd"), "turns": meta.get("num_turns")}
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- reporting ---------------------------------------------------------------
    def report(self) -> str:
        prices = self.prices()
        eq = self.book.equity(prices)
        trades = []
        if self.book.trades_csv.exists():
            with self.book.trades_csv.open(encoding="utf-8") as f:
                trades = list(csv.DictReader(f))
        wins = sum(1 for t in trades if float(t["pnl"]) > 0)
        try:
            btc = (self.market.price("BTC") / self.book.btc_start - 1) * 100 if self.book.btc_start else 0.0
        except Exception:
            btc = 0.0
        days = (self.clock() - self.book.started) / 86400
        return (f"📊 AI 交易員日報（模擬第 {days:.1f} 天）\n權益 {eq:.2f} U（{(eq / self.cfg['starting_cash'] - 1) * 100:+.1f}%），"
                f"同期 BTC {btc:+.1f}%\n已平倉 {len(trades)} 筆，賺 {wins} 筆\n\n倉位：\n{self.positions_text(prices)}")

    def maybe_report(self) -> None:
        tz = ZoneInfo(self.cfg["timezone"])
        now = datetime.fromtimestamp(self.clock(), tz)
        if not self.cfg["daily_report"] or now.strftime("%H:%M") < self.cfg["daily_report"]:
            return
        if self.last_report_day != now.date():
            self.last_report_day = now.date()
            self.notify(self.report())

    # -- main loop ---------------------------------------------------------------
    def step(self) -> None:
        now = self.clock()
        self.check_exits()
        self.funding()
        if now - self.last_news >= self.cfg["news_minutes"] * 60:
            first = self.last_news == 0
            new = self.news.poll()
            self.last_news = now
            if not first:
                self.pending_urgent += self.news.urgent(new)
        trigger = None
        if now - self.last_decision >= self.cfg["decide_minutes"] * 60:
            trigger = "定時檢查"
        elif self.pending_urgent and now - self.last_decision >= self.cfg["urgent_gap_minutes"] * 60:
            trigger = "突發新聞：" + " / ".join(i["title"] for i in self.pending_urgent[:5])
        if trigger:
            self.pending_urgent = []
            self.decide(trigger)
        self.maybe_report()

    def run(self) -> None:
        self.notify(f"🤖 AI 交易員（模擬）啟動，權益 {self.book.equity(self.prices()):.2f} U")
        while True:
            try:
                self.step()
            except Exception as exc:
                log.exception("step failed: %s", exc)
            time.sleep(60)


def main(cfg: dict, repo_dir: str, once: bool = False) -> int:
    import os

    from .data import make_exchange
    from .notify import Telegram

    token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    claude = Path(AI_ROOT) / "home" / ".local" / "bin" / "claude"
    if not token or not claude.exists():
        print("CLAUDE_CODE_OAUTH_TOKEN or the claude binary is missing")
        return 1
    c = {**DEFAULTS, **(cfg.get("aitrader") or {})}
    ai = TraderAI(cfg, repo_dir, str(claude), token, c["model"], int(c["max_turns"]))
    ai.install()
    tg_token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    notify = Telegram(tg_token, chat, prefix="[AI模擬] ") if tg_token and chat else None
    trader = AITrader(cfg, make_exchange(cfg), ai, notify)
    if once:
        trader.news.poll()
        trader.check_exits()
        trader.decide("手動測試")
        print(trader.journal.read_text(encoding="utf-8").splitlines()[-1])
        return 0
    trader.run()
    return 0
