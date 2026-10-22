"""台股 AI 建議：Claude 每個交易日盤前（08:30）和盤後（16:30）看國際盤、台指期、三大法人、融資融券、
月營收與新聞，對台灣50＋中型100（依市值前 150 檔上市普通股）給現股做多、台指期多空的短波段建議，推到 Telegram。

    python3 -m cryptobot tw-stock            # 服務：盤前／盤後決策，盤中每 5 分鐘檢查條件單、停損、目標
    python3 -m cryptobot tw-stock --once     # 現在做一次決策並推播（測試用）
    python3 -m cryptobot tw-stock --scan     # 只印出給 AI 的資料（不呼叫 AI），檢查資料源

只給建議、不下單。每個建議都記在一本虛擬帳（建議追蹤，預設 100 萬台幣）：AI 的進場在下一個開盤價成交
（跳空太多不追、開盤漲停買不到），盤中碰到停損／目標就出場，用來累積勝率與 R 的紀錄。
"""

from __future__ import annotations

import csv
import html
import json
import logging
import math
import re
import shutil
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .ai import AI_ROOT, ClaudeChat
from .aitrader import TraderAI

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Taipei")
UA = {"User-Agent": "Mozilla/5.0 (cryptobot tw-stock)"}
FUT = "TX"  # 台指期（虛擬帳以微型臺指計：每點 10 元）

DEFAULTS = {
    "capital": 1_000_000.0,  # 虛擬帳起始資金（台幣），建議張數照這個算
    "model": "opus",
    "max_turns": 30,
    "premarket": "08:30",
    "postmarket": "16:30",
    "universe": 150,
    "otc": 50,  # 另外加市值前幾大的上櫃股；0 = 不看上櫃
    "etfs": 20,  # 另外加成交值前幾大的股票型 ETF；0 = 不看 ETF
    "max_positions": 6,  # 持股＋等開盤成交的建議
    "max_risk_pct": 2.0,  # 打到停損虧掉的權益 %，每筆
    "max_open_risk_pct": 8.0,  # 全部倉位一起打到停損
    "max_position_pct": 30.0,  # 一檔股票的成本上限（現股，不用融資）
    "stock_stop_pct": [2.0, 15.0],
    "future_stop_pct": [0.4, 5.0],
    "max_gap_pct": 3.0,  # 開盤跳空超過這個 % 就不追
    "max_plans": 8,
    "plan_max_days": 10,  # 條件單最多有效幾個交易日
    "fee": 0.001425,
    "tax": 0.003,
    "future_point": 10.0,
    "future_cost_points": 2.0,  # 手續費＋期交稅＋滑價，每口來回約幾點
    "check_minutes": 5,
    "now_cooldown_seconds": 120,  # /now 防連按
}
INDUSTRY = {"01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織", "05": "電機", "06": "電器電纜", "08": "玻璃",
            "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造", "15": "航運", "16": "觀光",
            "17": "金融", "18": "貿易百貨", "19": "綜合", "20": "其他", "21": "化學", "22": "生技醫療", "23": "油電燃氣",
            "24": "半導體", "25": "電腦週邊", "26": "光電", "27": "通信網路", "28": "電子零組件", "29": "電子通路",
            "30": "資訊服務", "31": "其他電子", "32": "文化創意", "33": "農業科技", "34": "電子商務","35": "綠能環保", "36": "數位雲端", "37": "運動休閒", "38": "居家生活"}
GLOBAL = [  # (Yahoo symbol, 名稱)
    ("^GSPC", "標普500"), ("^IXIC", "那斯達克"), ("^SOX", "費城半導體"), ("^DJI", "道瓊"),
    ("ES=F", "標普期貨"), ("NQ=F", "那指期貨"), ("TSM", "台積電ADR"), ("NVDA", "輝達"), ("AAPL", "蘋果"),
    ("AMD", "超微"), ("AVGO", "博通"), ("MU", "美光"), ("^VIX", "VIX恐慌指數"), ("^TNX", "美10年債殖利率"),
    ("DX-Y.NYB", "美元指數"), ("TWD=X", "美元兌台幣"), ("JPY=X", "美元兌日圓"), ("^N225", "日經"),
    ("^KS11", "韓國綜合"), ("^HSI", "恆生"), ("000001.SS", "上證"), ("GC=F", "黃金"), ("CL=F", "原油"),
    ("BTC-USD", "比特幣"),
]
NEWS = {
    "鉅亨台股": "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=40",
    "鉅亨國際": "https://api.cnyes.com/media/api/v1/newslist/category/wd_stock?limit=30",
    "Yahoo台股": "https://tw.stock.yahoo.com/rss?category=tw-market",
    "CNBC": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
    "MarketWatch": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
}
WORK = Path(AI_ROOT) / "work" / "twstock"  # 給 AI 用 Read 讀的個股籌碼明細


def settings(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("twstock") or {})}


def _get(url: str, timeout: int = 20, data: bytes | None = None, headers: dict | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def num(x) -> float | None:
    try:
        v = float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def roc(s: str) -> str:
    """民國日期 1151021 → 20261021。"""
    s = s.replace("/", "")
    return f"{int(s[:-4]) + 1911}{s[-4:]}"


def md(d: str) -> str:
    return f"{int(d[4:6])}/{int(d[6:])}"


def fmt(x) -> str:
    if x is None:
        return "-"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    return f"{x:.2f}".rstrip("0").rstrip(".")


def sg(x, d: int = 1) -> str:
    return "-" if x is None else f"{x:+.{d}f}"


def pct(a, b) -> float | None:
    return (a / b - 1) * 100 if a and b else None


def sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def streak(vals: list[float]) -> int:
    """連買（+n）或連賣（−n）幾天，從最新一天往回數。"""
    n, sign = 0, 0
    for v in reversed(vals):
        s = (v > 0) - (v < 0)
        if s == 0 or (sign and s != sign):
            break
        sign, n = s, n + 1
    return n * sign


def structure(bars: list[dict], n: int = 10) -> str:
    if len(bars) < 2 * n:
        return "-"
    a, b = bars[-2 * n:-n], bars[-n:]
    hi = max(x["h"] for x in b) > max(x["h"] for x in a)
    lo = min(x["l"] for x in b) > min(x["l"] for x in a)
    return "升" if hi and lo else "降" if not hi and not lo else "盤"


# -- data -----------------------------------------------------------------------------
class Data:
    """公開資料：證交所、期交所、Yahoo、新聞。過去交易日的報表不會變，存檔快取在 state/twstock/。"""

    TWSE_GAP = 2.5  # 證交所網站每 5 秒超過約 3 次會封 IP

    def __init__(self, cache: Path, fetch=_get, clock=time.time, sleep=time.sleep):
        self.cache, self.fetch, self.clock, self.sleep = cache, fetch, clock, sleep
        cache.mkdir(parents=True, exist_ok=True)
        self.last_twse = 0.0
        self._holidays: set[str] | None = None
        f = cache / "otc_codes.json"  # 上櫃代號（Yahoo 用 .TWO、即時報價用 otc_）
        self.otc: set[str] = set(json.loads(f.read_text())) if f.exists() else set()

    def yahoo(self, code: str) -> str:
        if not re.fullmatch(r"\d{4,6}[A-Z]?", code):
            return code
        return f"{code}.TWO" if code in self.otc else f"{code}.TW"

    def tpex(self, path: str, name: str | None = None, day: str | None = None) -> dict:
        """櫃買中心網站報表（日期格式 2026/10/21），過去的日子存檔快取。"""
        f = self.cache / f"{name}.json" if name else None
        if f and f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        for attempt in range(3):
            try:
                d = self.json("https://www.tpex.org.tw/www/zh-tw/" + path, timeout=45)
                break
            except Exception as exc:
                log.warning("tpex %s: %s", path[:40], str(exc)[:100])
                if attempt == 2:
                    return {}
                self.sleep(3 * (attempt + 1))
        tables = d.get("tables") or []
        if day and (not tables or tables[0].get("date") != f"{int(day[:4]) - 1911}/{day[4:6]}/{day[6:]}"):
            return {}  # 那天還沒公布（或回的是別天的表）
        if f and tables and tables[0].get("data"):
            f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return d

    @staticmethod
    def slash(d: str) -> str:
        return f"{d[:4]}/{d[4:6]}/{d[6:]}"

    def otc_universe(self, n: int) -> list[dict]:
        """市值（收盤 × 實收資本額 ÷ 10）前 n 大的上櫃普通股。"""
        day = self._cached("otc_quotes", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", 3600 * 6)
        comp = {r["SecuritiesCompanyCode"]: r for r in self._cached(
            "otc_companies", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", 86400 * 3)}
        rows = []
        for r in day:
            code, c = r.get("SecuritiesCompanyCode", ""), comp.get(r.get("SecuritiesCompanyCode", ""))
            if not c or not re.fullmatch(r"[1-9]\d{3}", code):
                continue
            px, cap = num(r.get("Close")), num(c.get("Paidin.Capital.NTDollars"))
            if px and cap:
                ind = c.get("SecuritiesIndustryCode", "")
                rows.append({"code": code, "name": r["CompanyName"].strip(), "cap": px * cap / 10,
                             "industry": INDUSTRY.get(ind, ind) + "(櫃)"})
        rows.sort(key=lambda r: -r["cap"])
        rows = rows[:n]
        self.otc |= {r["code"] for r in rows}
        (self.cache / "otc_codes.json").write_text(json.dumps(sorted(self.otc)))
        return rows

    def json(self, url: str, **kw):
        return json.loads(self.fetch(url, **kw))

    def twse(self, path: str, name: str | None = None) -> dict:
        f = self.cache / f"{name}.json" if name else None
        if f and f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        for attempt in range(3):
            wait = self.TWSE_GAP * (1 + 2 * attempt) - (time.time() - self.last_twse)
            if wait > 0:
                self.sleep(wait)
            self.last_twse = time.time()
            try:
                d = self.json("https://www.twse.com.tw/rwd/zh/" + path, timeout=45)
                break
            except Exception as exc:
                log.warning("twse %s: %s", path[:40], str(exc)[:100])
                if attempt == 2:
                    return {"stat": "抓不到"}
        if f and d.get("stat") == "OK":
            f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return d

    def _cached(self, name: str, url: str, max_age: float):
        f = self.cache / f"{name}.json"
        if f.exists() and time.time() - f.stat().st_mtime < max_age:
            return json.loads(f.read_text(encoding="utf-8"))
        try:
            d = self.json(url)
        except Exception:
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
            raise
        f.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return d

    # -- calendar --
    def holidays(self) -> set[str]:
        if self._holidays is None:
            try:
                rows = self._cached("holidays", "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule",
                                    86400 * 3)
            except Exception as exc:
                log.warning("holidays: %s", exc)
                rows = []
            # 「開始交易日」「最後交易日」也列在表上，那兩天有開盤
            self._holidays = {roc(r["Date"]) for r in rows
                              if "開始交易" not in r.get("Name", "") and "最後交易" not in r.get("Name", "")}
        return self._holidays

    def trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d.strftime("%Y%m%d") not in self.holidays()

    def add_trading_days(self, d: date, n: int) -> date:
        while n > 0:
            d += timedelta(days=1)
            n -= self.trading_day(d)
        return d

    # -- universe --
    def universe(self, n: int) -> list[dict]:
        """市值（收盤 × 已發行股數）前 n 大的上市普通股 ≈ 台灣50 + 中型100。"""
        day = self._cached("stock_day_all", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", 3600 * 6)
        comp = {r["公司代號"]: r for r in self._cached(
            "companies", "https://openapi.twse.com.tw/v1/opendata/t187ap03_L", 86400 * 3)}
        rows = []
        for r in day:
            code, c = r.get("Code", ""), comp.get(r.get("Code", ""))
            if not c or not re.fullmatch(r"[1-9]\d{3}", code):
                continue
            px, sh = num(r.get("ClosingPrice")), num(c.get("已發行普通股數或TDR原股發行股數"))
            if px and sh:
                rows.append({"code": code, "name": r["Name"].strip(), "cap": px * sh,
                             "industry": INDUSTRY.get(c.get("產業別", ""), c.get("產業別", ""))})
        rows.sort(key=lambda r: -r["cap"])
        return rows[:n]

    NOT_EQUITY = re.compile(r"債|原油|黃金|白銀|黃豆|銅|美元|日圓|^期")

    def etfs(self, n: int) -> list[dict]:
        """成交值前 n 大的股票型 ETF（排除債券型 B、期貨型 U 和原物料、匯率的槓桿反向）。"""
        day = self._cached("stock_day_all", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", 3600 * 6)
        rows = []
        for r in day:
            code, name = r.get("Code", ""), r.get("Name", "").strip()
            if not re.fullmatch(r"00\d{2,4}[A-Z]?", code) or code[-1] in "BU" or self.NOT_EQUITY.search(name):
                continue
            kind = {"L": "ETF正2", "R": "ETF反1", "A": "主動ETF"}.get(code[-1], "ETF")
            rows.append({"code": code, "name": name, "industry": kind, "cap": num(r.get("TradeValue")) or 0})
        rows.sort(key=lambda r: -r["cap"])
        return rows[:n]

    def etf_premium(self) -> dict[str, float]:
        """ETF 盤中即時折溢價 %（成交價對預估淨值；盤後是收盤）。"""
        d = self.json("https://mis.twse.com.tw/stock/data/all_etf.txt")
        return {m["a"]: num(m.get("g")) for grp in d.get("a1", []) for m in grp.get("msgArray", [])
                if num(m.get("g")) is not None}

    # -- prices --
    def bars(self, symbol: str, rng: str = "1y", interval: str = "1d") -> list[dict]:
        sym = self.yahoo(symbol)
        d = self.json(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                      f"?range={rng}&interval={interval}")
        r = d["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        out = []
        stamps = r.get("timestamp") or []
        meta = r.get("meta") or {}
        for i, t in enumerate(stamps):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if c is None and i == len(stamps) - 1 and meta.get("regularMarketPrice") \
                    and meta.get("regularMarketTime", 0) >= t:
                # Yahoo 有時把最新一根留空，價格只放在 meta
                c = meta["regularMarketPrice"]
                h, l = meta.get("regularMarketDayHigh") or c, meta.get("regularMarketDayLow") or c
                o, v = o or c, v or meta.get("regularMarketVolume") or 0
            if None in (o, h, l, c):
                continue
            out.append({"t": t, "date": datetime.fromtimestamp(t, TZ).strftime("%Y%m%d"),
                        "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4), "v": v or 0})
        return out

    def many_bars(self, symbols: list[str], rng: str = "1y") -> dict[str, list[dict]]:
        def one(s):
            for attempt in range(3):
                try:
                    return s, self.bars(s, rng)
                except Exception as exc:
                    if attempt == 2:
                        log.warning("bars %s: %s", s, str(exc)[:120])
                    self.sleep(1 + attempt)
            return s, []
        with ThreadPoolExecutor(6) as pool:
            return {s: b for s, b in pool.map(one, symbols) if b}

    def quotes(self, codes: list[str]) -> dict[str, dict]:
        """證交所盤中即時（o 開、h 高、l 低、z 最新、y 昨收、u 漲停、w 跌停）。"""
        out = {}
        for i in range(0, len(codes), 50):
            ch = "|".join(f"{'otc' if c in self.otc or c == 'o00' else 'tse'}_{c}.tw" for c in codes[i:i + 50])
            d = self.json("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=" + ch)
            for m in d.get("msgArray", []):
                z = num(m.get("z")) or num((m.get("trade") or {}).get("z"))
                out[m["c"]] = {"date": m.get("d"), "o": num(m.get("o")), "h": num(m.get("h")), "l": num(m.get("l")),
                               "z": z, "y": num(m.get("y")), "u": num(m.get("u")), "w": num(m.get("w")),
                               "v": num(m.get("v"))}
        return out

    def tx_quote(self) -> dict | None:
        """台指期日盤即時，取成交量最大的月份。"""
        body = json.dumps({"MarketType": "0", "SymbolType": "F", "KindID": "1", "CID": "TXF", "ExpireMonth": "",
                           "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}).encode()
        d = self.json("https://mis.taifex.com.tw/futures/api/getQuoteList", data=body,
                      headers={"Content-Type": "application/json"})
        rows = [r for r in d["RtData"]["QuoteList"] if r["SymbolID"].endswith("-F")]
        if not rows:
            return None
        r = max(rows, key=lambda r: num(r.get("CTotalVolume")) or 0)
        return {"date": r.get("CDate"), "o": num(r.get("COpenPrice")), "h": num(r.get("CHighPrice")),
                "l": num(r.get("CLowPrice")), "z": num(r.get("CLastPrice")), "y": num(r.get("CRefPrice")),
                "u": None, "w": None, "name": r.get("DispCName")}

    # -- chips --
    def chips(self, d: str) -> dict[str, tuple[float, float, float]]:
        """三大法人個股買賣超（股）：外資、投信、自營商。"""
        out = {}
        t = self.twse(f"fund/T86?date={d}&selectType=ALLBUT0999&response=json", f"T86_{d}")
        if t.get("stat") == "OK":
            f = t["fields"]
            fi, ti, di = (f.index("外陸資買賣超股數(不含外資自營商)"), f.index("投信買賣超股數"), f.index("自營商買賣超股數"))
            out = {r[0].strip(): (num(r[fi]) or 0, num(r[ti]) or 0, num(r[di]) or 0) for r in t["data"]}
        if self.otc:
            # 上櫃：外資(不含外資自營商)買賣超在第 4 欄、投信第 13 欄、自營商合計第 22 欄
            o = self.tpex(f"insti/dailyTrade?type=Daily&sect=EW&date={self.slash(d)}&response=json", f"OTC3I_{d}", d)
            for r in ((o.get("tables") or [{}])[0].get("data") or []):
                if len(r) >= 23:
                    out[r[0].strip()] = (num(r[4]) or 0, num(r[13]) or 0, num(r[22]) or 0)
        return out

    def margin(self, d: str) -> tuple[dict[str, tuple[float, float]], float | None]:
        """個股融資、融券餘額（張）＋全市場融資金額（仟元）。"""
        out, total = {}, None
        if self.otc:
            o = self.tpex(f"margin/balance?date={self.slash(d)}&response=json", f"OTCMARGN_{d}", d)
            t = (o.get("tables") or [{}])[0]
            f = t.get("fields") or []
            if "資餘額" in f and "券餘額" in f:
                mi, si = f.index("資餘額"), f.index("券餘額")
                out.update({r[0].strip(): (num(r[mi]) or 0, num(r[si]) or 0) for r in t.get("data") or []})
        m = self.twse(f"marginTrading/MI_MARGN?date={d}&selectType=ALL&response=json", f"MARGN_{d}")
        if m.get("stat") != "OK":
            return out, None
        for t in m.get("tables", []):
            f, rows = t.get("fields") or [], t.get("data") or []
            if "項目" in f:
                for r in rows:
                    if r[0].startswith("融資金額"):
                        total = num(r[f.index("今日餘額")])
            elif f[:1] == ["代號"]:
                bal = [i for i, x in enumerate(f) if x == "今日餘額"]
                if len(bal) >= 2:
                    out.update({r[0].strip(): (num(r[bal[0]]) or 0, num(r[bal[1]]) or 0) for r in rows})
        return out, total

    def institutions(self, d: str) -> dict[str, float]:
        """三大法人現貨買賣超總計（元）。"""
        b = self.twse(f"fund/BFI82U?dayDate={d}&type=day&response=json", f"BFI82U_{d}")
        return {r[0]: num(r[3]) or 0 for r in b.get("data", [])} if b.get("stat") == "OK" else {}

    def last_days(self, now: datetime, n: int) -> list[str]:
        """最近 n 個已收盤的交易日（舊到新），照休市日曆算。"""
        d = now.date()
        if not (self.trading_day(d) and now.strftime("%H:%M") >= "14:00"):
            d -= timedelta(days=1)
            while not self.trading_day(d):
                d -= timedelta(days=1)
        out = [d]
        while len(out) < n:
            d -= timedelta(days=1)
            if self.trading_day(d):
                out.append(d)
        return [x.strftime("%Y%m%d") for x in reversed(out)]

    def index_bars(self, d: str) -> list[dict]:
        """證交所的加權指數日線（這個月和上個月），用來補 Yahoo 漏掉的日子。"""
        out = []
        first = date(int(d[:4]), int(d[4:6]), 1)
        for m in (first - timedelta(days=1)).replace(day=1), first:
            t = self.twse(f"TAIEX/MI_5MINS_HIST?date={m:%Y%m}01&response=json",
                          f"TAIEX_{m:%Y%m}" if m < first else f"TAIEX_{d}")
            for r in t.get("data", []) if t.get("stat") == "OK" else []:
                o, h, l, c = (num(x) for x in r[1:5])
                if None not in (o, h, l, c):
                    day = roc(r[0])
                    out.append({"t": datetime.strptime(day, "%Y%m%d").replace(hour=9, tzinfo=TZ).timestamp(),
                                "date": day, "o": o, "h": h, "l": l, "c": c, "v": 0})
        return out

    def turnover(self, d: str) -> dict[str, float]:
        """這個月和上個月每天的成交金額（元）。"""
        out = {}
        first = date(int(d[:4]), int(d[4:6]), 1)
        for m in (first - timedelta(days=1)).replace(day=1), first:
            past = m < first
            t = self.twse(f"afterTrading/FMTQIK?date={m:%Y%m}01&response=json",
                          f"FMTQIK_{m:%Y%m}" if past else f"FMTQIK_{d}")
            for r in t.get("data", []) if t.get("stat") == "OK" else []:
                out[roc(r[0])] = num(r[2]) or 0
        return out

    def futures(self) -> dict:
        """台指期近月日盤／夜盤、三大法人台指期淨未平倉（期交所只給最新一天，歷史存在這裡）。"""
        rows = self.json("https://openapi.taifex.com.tw/v1/DailyMarketReportFut")
        tx = [r for r in rows if r.get("Contract") == "TX" and re.fullmatch(r"\d{6}", r.get("ContractMonth(Week)", ""))]
        reg = [r for r in tx if r.get("TradingSession") == "一般"]
        out: dict = {}
        if reg:
            near = max(reg, key=lambda r: num(r.get("OpenInterest")) or 0)
            month = near["ContractMonth(Week)"]
            night = [r for r in tx if r["ContractMonth(Week)"] == month and r.get("TradingSession") == "盤後"]
            out = {"date": near["Date"], "month": month, "day": near, "night": night[0] if night else None}
        oi = self.json("https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate")
        net = {r["Item"]: num(r.get("OpenInterest(Net)")) for r in oi if r.get("ContractCode") == "臺股期貨"}
        f = self.cache / "futures_oi.json"
        hist = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        if oi and net:
            hist[oi[0]["Date"]] = net
            hist = dict(sorted(hist.items())[-30:])
            f.write_text(json.dumps(hist, ensure_ascii=False), encoding="utf-8")
        out["oi"] = hist
        return out

    def revenue(self) -> dict[str, dict]:
        rows = list(self._cached("revenue", "https://openapi.twse.com.tw/v1/opendata/t187ap05_L", 3600 * 6))
        if self.otc:
            try:
                rows += self._cached("otc_revenue", "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O", 3600 * 6)
            except Exception as exc:
                log.warning("otc revenue: %s", exc)
        return {r["公司代號"]: {"ym": r.get("資料年月", ""), "mom": num(r.get("營業收入-上月比較增減(%)")),
                               "yoy": num(r.get("營業收入-去年同月增減(%)")),
                               "cum": num(r.get("累計營業收入-前期比較增減(%)"))} for r in rows}

    def alerts(self) -> dict[str, str]:
        out = {}
        for name, url in (("處置", "https://openapi.twse.com.tw/v1/announcement/punish"),
                          ("注意", "https://openapi.twse.com.tw/v1/announcement/notice")):
            try:
                for r in self.json(url):
                    if r.get("Code"):
                        out.setdefault(r["Code"], f"處置中{r.get('DispositionPeriod', '')}" if name == "處置" else "注意股")
            except Exception as exc:
                log.warning("alerts %s: %s", name, exc)
        if self.otc:
            try:
                for r in self.json("https://www.tpex.org.tw/openapi/v1/tpex_disposal_information"):
                    if r.get("SecuritiesCompanyCode"):
                        out.setdefault(r["SecuritiesCompanyCode"], f"處置中{r.get('DispositionPeriod', '')}")
            except Exception as exc:
                log.warning("alerts otc: %s", exc)
        return out

    def news(self, hours: float = 36) -> list[dict]:
        got = []
        cutoff = self.clock() - hours * 3600
        for src, url in NEWS.items():
            try:
                raw = self.fetch(url)
                if "cnyes" in url:
                    for a in json.loads(raw)["items"]["data"]:
                        got.append({"src": src, "title": html.unescape(a["title"]), "ts": a["publishAt"]})
                    continue
                for it in ET.fromstring(raw).iter("item"):
                    title = (it.findtext("title") or "").strip()
                    try:
                        ts = datetime.strptime((it.findtext("pubDate") or "").strip()[:25],
                                               "%a, %d %b %Y %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
                    except ValueError:
                        ts = self.clock()
                    if title:
                        got.append({"src": src, "title": title, "ts": ts})
            except Exception as exc:
                log.warning("news %s: %s", src, str(exc)[:120])
        seen, out = set(), []
        for g in sorted(got, key=lambda g: -g["ts"]):
            if g["ts"] > cutoff and g["title"] not in seen:
                seen.add(g["title"])
                out.append(g)
        return out


# -- the scan ---------------------------------------------------------------------------
def scan_stock(bars: list[dict], idx20: float | None, chips: list[tuple], margin: list[tuple],
               rev: dict | None, alert: str | None) -> dict | None:
    """一檔股票的掃描數字。chips／margin：最近幾個交易日（舊到新）每天的 (外資, 投信, 自營) 股數、(融資, 融券) 張數。"""
    if len(bars) < 22:  # 新上市（例如主動型 ETF）不到 60 天的，60 日相關欄位留空
        return None
    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    close = c[-1]
    ma5, ma20, ma60 = sma(c, 5), sma(c, 20), sma(c, 60)
    prev = bars[-61:-1]
    tr = [max(b["h"], p["c"]) - min(b["l"], p["c"]) for p, b in zip(bars[-15:-1], bars[-14:])]
    f = [x[0] / 1000 for x in chips]
    t = [x[1] / 1000 for x in chips]
    d = [x[2] / 1000 for x in chips]
    vol5 = sum(v[-5:]) / 1000
    s = {
        "close": close, "d1": pct(c[-1], c[-2]), "d5": pct(c[-1], c[-6]), "d20": pct(c[-1], c[-21]),
        "d60": pct(c[-1], c[-61]) if len(c) > 60 else None,
        "ma20": pct(close, ma20), "ma60": pct(close, ma60), "ma240": pct(close, sma(c, 240)),
        "trend": "-" if ma60 is None else "多頭排列" if ma5 > ma20 > ma60 else "空頭排列" if ma5 < ma20 < ma60 else "糾結",
        "struct": structure(bars),
        "hi60": pct(close, max(b["h"] for b in prev)), "lo60": pct(close, min(b["l"] for b in prev)),
        "hi250": pct(close, max(b["h"] for b in bars[-251:-1])),
        "volx": v[-1] / (sum(v[-21:-1]) / 20) if sum(v[-21:-1]) else None,
        "atr": sum(tr) / len(tr) / close * 100 if tr else None,
        "f5": sum(f[-5:]), "fs": streak(f), "t5": sum(t[-5:]), "ts": streak(t), "dl5": sum(d[-5:]),
        "inst": (sum(f[-5:]) + sum(t[-5:])) / vol5 * 100 if vol5 and chips else None,
        "m5": pct(margin[-1][0], margin[-6][0]) if len(margin) >= 6 else None,
        "sr": margin[-1][1] / margin[-1][0] * 100 if margin and margin[-1][0] else None,
        "rev": rev, "alert": alert,
    }
    s["rs20"] = s["d20"] - idx20 if s["d20"] is not None and idx20 is not None else None
    flags = []
    if s["hi60"] is not None and s["hi60"] > 0 and (s["volx"] or 0) >= 1.5:
        flags.append("帶量突破60日高")
    if s["ts"] >= 3:
        flags.append(f"投信連買{s['ts']}天")
    if abs(s["fs"]) >= 4:
        flags.append(f"外資連{'買' if s['fs'] > 0 else '賣'}{abs(s['fs'])}天")
    if s["f5"] > 0 and s["t5"] > 0 and (s["inst"] or 0) >= 10:
        flags.append("土洋同買")
    if len(c) > 21 and c[-2] < sma(c[:-1], 20) <= close:
        flags.append("站回月線")
    if len(c) > 61 and c[-2] >= sma(c[:-1], 60) > close:
        flags.append("跌破季線")
    if (s["m5"] or 0) > 5 and (s["d5"] or 0) < -3:
        flags.append("融資增價跌")
    if (s["d1"] or 0) < -4 and (s["volx"] or 0) > 2:
        flags.append("爆量長黑")
    if ma60 is not None and ma20 > ma60 and abs(s["ma20"]) < 1.5 and (s["volx"] or 1) < 0.8:
        flags.append("量縮回測月線")
    if rev and (rev.get("yoy") or 0) > 30:
        flags.append("營收年增>30%")
    if alert:
        flags.append(alert)
    s["flags"] = flags
    return s


def scan_line(u: dict, s: dict) -> str:
    rev = s["rev"] or {}
    revs = f"{sg(rev.get('yoy'), 0)}/{sg(rev.get('cum'), 0)}" if rev else "-"
    return (f"{u['code']} {u['name']}|{u['industry']}|{fmt(s['close'])}|{sg(s['d1'])}/{sg(s['d5'])}/{sg(s['d20'])}/"
            f"{sg(s['d60'])}|{sg(s['rs20'])}|{sg(s['ma20'])}/{sg(s['ma60'])}/{sg(s['ma240'])}|{s['trend']}|{s['struct']}|"
            f"{sg(s['hi60'])}/{sg(s['lo60'])}/{sg(s['hi250'])}|{s['volx'] or 0:.1f}x|{s['atr'] or 0:.1f}|"
            f"{s['f5']:+,.0f}({s['fs']:+d})|{s['t5']:+,.0f}({s['ts']:+d})|{s['dl5']:+,.0f}|{sg(s['inst'], 0)}|"
            f"{sg(s['m5'])}|{fmt(s['sr'])}|{revs}|{','.join(s['flags'])}")


SCAN_HEADER = ("代號 名稱|產業|收盤|漲跌%1日/5日/20日/60日|20日相對大盤|距月線/季線/年線%|均線排列|日線結構|"
               "距60日高/60日低/52週高%|量比|ATR%|外資5日張(連買賣)|投信5日張(連買賣)|自營5日張|法人佔量%|"
               "融資5日%|券資比|營收年增/累計年增%|訊號")


# -- the virtual book (建議追蹤) ------------------------------------------------------------
class Book:
    """建議的虛擬帳：股票只做多（現股，不用融資），台指期以微台指計（每點 10 元）。"""

    FIELDS = ["code", "name", "side", "qty", "entry", "exit", "init_stop", "opened", "closed", "pnl", "r", "why", "reason"]

    def __init__(self, path: Path, trades_csv: Path, cfg: dict):
        self.path, self.trades_csv, self.cfg = path, trades_csv, cfg
        d = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.realized = d.get("realized", 0.0)
        self.positions: dict[str, dict] = d.get("positions", {})
        self.pending: list[dict] = d.get("pending", [])  # 等下一個開盤執行：{"type": "open"/"close", ...}
        self.plans: list[dict] = d.get("plans", [])

    def save(self) -> None:
        self.path.write_text(json.dumps({"realized": self.realized, "positions": self.positions,
                                         "pending": self.pending, "plans": self.plans},
                                        ensure_ascii=False, indent=1), encoding="utf-8")

    @property
    def capital(self) -> float:
        return float(self.cfg["capital"])

    def mult(self, code: str) -> float:
        return self.cfg["future_point"] if code == FUT else 1.0

    def pnl(self, p: dict, px: float, qty: float | None = None) -> float:
        qty = p["qty"] if qty is None else qty
        if p["code"] == FUT:
            sgn = 1 if p["side"] == "long" else -1
            return ((px - p["entry"]) * sgn - self.cfg["future_cost_points"]) * qty * self.cfg["future_point"]
        c = self.cfg
        return (px - p["entry"]) * qty - c["fee"] * p["entry"] * qty - (c["fee"] + c["tax"]) * px * qty

    def equity(self, prices: dict[str, float]) -> float:
        return self.capital + self.realized + sum(self.pnl(p, prices.get(code, p["entry"]))
                                                  for code, p in self.positions.items())

    def risk_at_stop(self, p: dict) -> float:
        """打到停損會虧多少（停損已經鎖利的倉位算 0）。"""
        return max(-self.pnl(p, p["stop"]), 0.0)

    def size(self, code: str, side: str, entry: float, stop: float, risk_pct: float, equity: float) -> tuple[int, str]:
        """(數量, 被哪個上限卡住)。股票是股數（可以買零股），台指期是微台口數。"""
        per = abs(entry - stop) * self.mult(code)
        qty = int(equity * risk_pct / 100 / per) if per else 0
        why = ""
        if code != FUT:
            cost = sum(p["entry"] * p["qty"] for c, p in self.positions.items() if c != FUT)
            cap = int(min(equity * self.cfg["max_position_pct"] / 100, max(equity - cost, 0)) / entry)
            if cap < qty:
                qty, why = cap, f"（受單檔 {self.cfg['max_position_pct']:g}% 或現金上限）"
        return qty, why

    def open(self, a: dict, entry: float, now: float, equity: float, intraday: bool = False) -> dict:
        """intraday=True：盤中才成交（不是開盤價），當天稍早的高低點不能拿來判斷停損。"""
        qty, why = self.size(a["code"], a["side"], entry, a["stop"], a["risk_pct"], equity)
        if qty < 1:
            raise ValueError("算出來的數量不到 1（停損太遠或資金不夠）")
        p = {"code": a["code"], "name": a.get("name", ""), "side": a["side"], "qty": qty, "entry": entry,
             "stop": a["stop"], "init_stop": a["stop"], "tp": a.get("tp"), "opened": now,
             "reason": a.get("reason", ""), "risk_pct": a["risk_pct"], "note": why, "intraday": intraday}
        self.positions[a["code"]] = p
        self.save()
        return p

    def close(self, code: str, px: float, why: str, now: float) -> dict:
        p = self.positions.pop(code)
        pnl = self.pnl(p, px)
        risk = abs(p["entry"] - p["init_stop"]) * p["qty"] * self.mult(code)
        self.realized += pnl
        self.save()
        row = {"code": code, "name": p.get("name", ""), "side": p["side"], "qty": p["qty"], "entry": p["entry"],
               "exit": px, "init_stop": p["init_stop"], "opened": tw(p["opened"]), "closed": tw(now),
               "pnl": round(pnl), "r": round(pnl / risk, 2) if risk else 0, "why": why, "reason": p.get("reason", "")}
        new = not self.trades_csv.exists()
        with self.trades_csv.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        return row

    def trades(self) -> list[dict]:
        if not self.trades_csv.exists():
            return []
        with self.trades_csv.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))


def tw(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%m/%d %H:%M")


def label(code: str, name: str = "") -> str:
    return "台指期" if code == FUT else f"{code} {name}".strip()


def lots(qty: float) -> str:
    q = int(qty)
    return f"{q // 1000} 張" + (f" {q % 1000} 股" if q % 1000 else "") if q >= 1000 else f"{q} 股（零股）"


def qty_text(code: str, qty: float) -> str:
    return f"{int(qty)} 口微台" if code == FUT else lots(qty)


# -- the advisor ------------------------------------------------------------------------
class TWStockAI(TraderAI):
    PROMPT = "twstock-prompt.md"

    def install(self) -> None:
        super().install()
        dest = self.tools_dir / "twmarket.py"
        shutil.copyfile(self.repo_dir / "deploy" / "ai" / "twmarket.py", dest)
        dest.chmod(0o644)


class TWChat(TWStockAI):
    """台股聊天室的問答：同一段對話接著問（--resume），超過 idle 沒問就開新對話。只回答、不執行任何動作。"""

    def __init__(self, *a, idle_hours: float = 2, **kw):
        super().__init__(*a, **kw)
        self.idle = idle_hours * 3600

    def command(self, prompt: str) -> list[str]:
        cmd = super().command(prompt)
        return cmd + ["--resume", self.session_id] if self.session_id else cmd

    def ask(self, prompt: str) -> str:
        note = ""
        if self.session_id and self.clock() - self.last_at > self.idle:
            self.session_id = None
            note = f"🆕 超過 {self.idle / 3600:g} 小時沒問，開新對話\n\n"
        resumed = self.session_id is not None
        data = self._call(prompt)
        if data.get("is_error") and resumed and "session" in str(data.get("result", "")).lower():
            self.session_id = None
            data = self._call(prompt)
        self.last_at = self.clock()
        if data.get("session_id"):
            self.session_id = data["session_id"]
        result = str(data.get("result") or "").strip()
        if data.get("subtype") == "error_max_turns":
            return note + (result or "⚠️ 查的步驟太多被中斷，問得更具體一點再試")
        if data.get("is_error"):
            return note + f"⚠️ AI 回覆失敗：{result[:300] or data.get('subtype')}"
        return note + (result or "（AI 沒有回覆內容）")


ACTION_NAMES = {"open": "進場", "close": "出場", "move_stop": "移動停損", "take_profit": "改目標",
                "plan": "條件單", "cancel_plan": "取消條件單"}


class Advisor:
    def __init__(self, cfg: dict, data: Data, ai, notify=None, clock=time.time, work_dir: Path = WORK):
        self.cfg = settings(cfg)
        state, logs = Path(cfg.get("state_dir", "state")), Path(cfg.get("log_dir", "logs"))
        state.mkdir(parents=True, exist_ok=True)
        logs.mkdir(parents=True, exist_ok=True)
        self.data, self.ai, self.clock, self.work_dir = data, ai, clock, work_dir
        self.notify = notify or (lambda text: None)
        self.book = Book(state / "twstock_book.json", logs / "twstock_trades.csv", self.cfg)
        self.journal = logs / "twstock_decisions.jsonl"
        self.flags_path = state / "twstock_flags.json"
        self.flags: dict = json.loads(self.flags_path.read_text()) if self.flags_path.exists() else {}
        self.names: dict[str, str] = {}
        self.closes: dict[str, float] = {}  # 最近一次決策時每檔的收盤（盤中即時決策時是現價）＝建議的參考價
        self.last_check = 0.0
        self.live_now = False  # 盤中即時決策：open／close 以現價立即成交
        self.last_now = 0.0
        self.commands = None  # Commands：台股機器人的 /now、/tw
        self.chat = None  # TWChat：問答

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), TZ)

    def set_flag(self, k: str, v) -> None:
        self.flags[k] = v
        self.flags_path.write_text(json.dumps(self.flags))

    # -- building the prompt --
    def gather(self) -> dict:
        d = self.data
        uni = d.universe(self.cfg["universe"])
        if self.cfg["otc"]:
            try:
                uni += d.otc_universe(self.cfg["otc"])
            except Exception as exc:
                log.warning("otc universe: %s", exc)
        if self.cfg["etfs"]:
            try:
                uni += [e for e in d.etfs(self.cfg["etfs"]) if e["code"] not in {u["code"] for u in uni}]
            except Exception as exc:
                log.warning("etfs: %s", exc)
        try:
            premium = d.etf_premium()
        except Exception as exc:
            log.warning("etf premium: %s", exc)
            premium = {}
        self.names = {u["code"]: u["name"] for u in uni}
        bars = d.many_bars([u["code"] for u in uni] + ["^TWII"])
        now = self.now()
        if now.strftime("%H:%M") < "14:00":  # 盤中：今天那根還沒收完，不算
            today = now.strftime("%Y%m%d")
            bars = {k: [x for x in v if x["date"] != today] for k, v in bars.items()}
        idx = bars.pop("^TWII", [])
        days = d.last_days(now, 10)
        try:  # Yahoo 的加權指數偶爾漏一天，用證交所的補
            have = {b["date"] for b in idx}
            idx = sorted(idx + [b for b in d.index_bars(days[-1]) if b["date"] not in have and b["date"] <= days[-1]],
                         key=lambda b: b["date"])
        except Exception as exc:
            log.warning("index bars: %s", exc)
        chips = {day: d.chips(day) for day in days}
        margins = {day: d.margin(day) for day in days[-7:]}  # 當天的融資融券晚上才公布
        rev, alerts = d.revenue(), d.alerts()
        idx20 = pct(idx[-1]["c"], idx[-21]["c"]) if len(idx) > 21 else None
        rows, detail = [], {}
        for u in uni:
            b = bars.get(u["code"])
            if not b:
                continue
            ch = [chips[day].get(u["code"], (0, 0, 0)) for day in days if chips[day]]
            mg = [margins[day][0][u["code"]] for day in days[-7:] if u["code"] in margins[day][0]]
            s = scan_stock(b, idx20, ch, mg, rev.get(u["code"]), alerts.get(u["code"]))
            if s and "ETF" in u["industry"]:
                p = premium.get(u["code"])
                s["flags"].append(f"折溢價{p:+.2f}%" if p is not None else "折溢價-")
            if s:
                rows.append((u, s))
                self.closes[u["code"]] = s["close"]
                detail[u["code"]] = {
                    "name": u["name"],
                    "days": [{"date": day, "close": next((x["c"] for x in b if x["date"] == day), None),
                              "外資張": round(chips[day].get(u["code"], (0, 0, 0))[0] / 1000),
                              "投信張": round(chips[day].get(u["code"], (0, 0, 0))[1] / 1000),
                              "自營張": round(chips[day].get(u["code"], (0, 0, 0))[2] / 1000),
                              "融資張": margins[day][0].get(u["code"], (None, None))[0] if day in margins else None,
                              "融券張": margins[day][0].get(u["code"], (None, None))[1] if day in margins else None}
                             for day in days if chips[day]],
                    "月營收": rev.get(u["code"]), "警示": alerts.get(u["code"]),
                }
        glob = d.many_bars([s for s, _ in GLOBAL], "3mo")
        try:
            fut = d.futures()
        except Exception as exc:
            log.warning("futures: %s", exc)
            fut = {}
        try:
            turnover = d.turnover(days[-1]) if days else {}
        except Exception as exc:
            log.warning("turnover: %s", exc)
            turnover = {}
        inst = {day: x for day in days[-5:] if (x := d.institutions(day))}  # 當天還沒公布的不列
        if fut.get("day"):
            # 盤前用夜盤收盤當台指期參考價（比前一天日盤新）
            night = fut.get("night") if self.now().strftime("%H:%M") < "13:45" else None
            self.closes[FUT] = num((night or fut["day"]).get("Last")) or num(fut["day"].get("Last"))
        return {"uni": uni, "rows": rows, "detail": detail, "idx": idx, "days": days, "glob": glob, "fut": fut,
                "turnover": turnover, "inst": inst, "margin_total": {k: v[1] for k, v in margins.items()},
                "news": d.news(), "bars": bars}

    def market_text(self, g: dict) -> str:
        idx, out = g["idx"], []
        if len(idx) > 61:
            c = [b["c"] for b in idx]
            out.append(f"加權指數 {fmt(c[-1])}（{md(idx[-1]['date'])}，{sg(pct(c[-1], c[-2]), 2)}%），5日 {sg(pct(c[-1], c[-6]))}%、"
                       f"20日 {sg(pct(c[-1], c[-21]))}%；距月線 {sg(pct(c[-1], sma(c, 20)))}%、季線 {sg(pct(c[-1], sma(c, 60)))}%、"
                       f"年線 {sg(pct(c[-1], sma(c, 240)))}%；日線結構 {structure(idx)}；距52週高 {sg(pct(c[-1], max(c[-250:])))}%")
            out.append("加權指數近 10 日（日期 開 高 低 收）：" + "；".join(
                f"{md(b['date'])} {fmt(b['o'])} {fmt(b['h'])} {fmt(b['l'])} {fmt(b['c'])}" for b in idx[-10:]))
        tv = g["turnover"]
        if tv:
            vals = [tv[k] for k in sorted(tv)]
            out.append(f"上市成交金額：最近 {fmt(vals[-1] / 1e8)} 億（20 日平均 {fmt(sum(vals[-20:]) / len(vals[-20:]) / 1e8)} 億）")
        inst = g["inst"]
        if inst:
            names = [("外資", "外資及陸資(不含外資自營商)"), ("投信", "投信"), ("自營商", None), ("合計", "合計")]
            parts = []
            for nm, key in names:
                vals = []
                for day in sorted(inst):
                    x = inst[day]
                    v = (x.get("自營商(自行買賣)", 0) + x.get("自營商(避險)", 0)) if key is None else x.get(key)
                    vals.append(f"{(v or 0) / 1e8:+.0f}")
                parts.append(f"{nm} {' '.join(vals)}")
            out.append(f"三大法人現貨買賣超（億元，{md(sorted(inst)[0])}–{md(sorted(inst)[-1])}，舊到新）：" + "｜".join(parts))
        mt = {k: v for k, v in g["margin_total"].items() if v}
        if mt:
            ks = sorted(mt)
            out.append(f"融資餘額 {mt[ks[-1]] / 1e5:,.0f} 億（{md(ks[0])} 起變化 {(mt[ks[-1]] - mt[ks[0]]) / 1e5:+,.0f} 億）")
        fut = g["fut"]
        if fut.get("day"):
            dd, nn = fut["day"], fut.get("night")
            line = (f"台指期 {fut['month'][4:]} 月：{md(fut['date'])} 日盤收 {fmt(num(dd['Last']))}（{dd['Change']}）")
            if nn:
                line += f"，夜盤收 {fmt(num(nn['Last']))}（{nn['Change']}，高 {fmt(num(nn['High']))} 低 {fmt(num(nn['Low']))}）"
            if idx and idx[-1]["date"] == fut["date"]:
                line += f"；期現貨價差 {num(dd['Last']) - idx[-1]['c']:+,.0f} 點"
            out.append(line)
        oi = fut.get("oi") or {}
        if oi:
            ks = sorted(oi)[-6:]
            out.append("三大法人台指期淨未平倉（口，舊到新）：" + "｜".join(
                f"{nm} " + " ".join(f"{(oi[k].get(key) or 0):+,.0f}" for k in ks)
                for nm, key in (("外資", "外資及陸資"), ("投信", "投信"), ("自營商", "自營商"))) + f"（{md(ks[0])}–{md(ks[-1])}）")
        return "\n".join(out)

    def global_text(self, g: dict) -> str:
        out = ["名稱 代號|最後收盤|1日%|5日%|20日%|最後一根（台北時間）"]
        glob = g["glob"]
        for sym, name in GLOBAL:
            b = glob.get(sym)
            if not b or len(b) < 22:
                continue
            c = [x["c"] for x in b]
            out.append(f"{name} {sym}|{fmt(c[-1])}|{sg(pct(c[-1], c[-2]), 2)}|{sg(pct(c[-1], c[-6]))}|{sg(pct(c[-1], c[-21]))}|"
                       f"{datetime.fromtimestamp(b[-1]['t'], TZ):%m/%d %H:%M}")
        tsm, usd, tw2330 = glob.get("TSM"), glob.get("TWD=X"), g["bars"].get("2330")
        if tsm and usd and tw2330:
            prem = pct(tsm[-1]["c"] * usd[-1]["c"] / 5, tw2330[-1]["c"])
            out.append(f"台積電 ADR 溢價（ADR×匯率÷5 對 2330 最近收盤 {fmt(tw2330[-1]['c'])}）：{sg(prem)}%")
        return "\n".join(out)

    def in_session(self) -> bool:
        now = self.now()
        return self.data.trading_day(now.date()) and "09:00" <= now.strftime("%H:%M") < "13:25"

    def live_text(self, g: dict) -> str:
        """盤中即時：加權指數、台指期、150 檔的現價；同時把參考價換成現價。"""
        today = self.now().strftime("%Y%m%d")
        q = self.data.quotes([u["code"] for u in g["uni"]] + ["t00", "o00"])
        out = []
        for key, nm in (("t00", "加權指數"), ("o00", "櫃買指數")):
            ix = q.get(key)
            if ix and ix.get("z") and ix.get("y"):
                out.append(f"{nm} {fmt(ix['z'])}（{sg(pct(ix['z'], ix['y']), 2)}%），開 {fmt(ix['o'])} 高 {fmt(ix['h'])} 低 {fmt(ix['l'])}")
        try:
            tx = self.data.tx_quote()
        except Exception as exc:
            log.warning("tx quote: %s", exc)
            tx = None
        if tx and tx.get("z"):
            self.closes[FUT] = tx["z"]
            out.append(f"台指期 {tx.get('name', '')} {fmt(tx['z'])}（{sg(pct(tx['z'], tx['y']), 2)}%），"
                       f"開 {fmt(tx['o'])} 高 {fmt(tx['h'])} 低 {fmt(tx['l'])}")
        rows = []
        for u in g["uni"]:
            x = q.get(u["code"])
            if not x or x.get("date") != today or not x.get("z"):
                continue
            self.closes[u["code"]] = x["z"]
            b = g["bars"].get(u["code"]) or []
            avg = sum(v["v"] for v in b[-20:]) / 20 / 1000 if len(b) >= 20 else None
            chg = pct(x["z"], x["y"])
            mark = ("漲停" if x.get("u") and x["z"] >= x["u"] else "跌停" if x.get("w") and x["z"] <= x["w"] else "")
            ratio = f"{x['v'] / avg:.1f}x" if x.get("v") and avg else "-"
            rows.append((chg or 0, f"{u['code']} {u['name']}|{fmt(x['z'])}|{sg(chg)}|{fmt(x['o'])}/{fmt(x['h'])}/"
                                   f"{fmt(x['l'])}|{fmt(x.get('v'))}|{ratio}|{mark}"))
        rows.sort(key=lambda r: -r[0])
        out.append("代號 名稱|現價|漲跌%|開/高/低|今日累計量(張)|累計量÷20日均量|漲跌停")
        out += [r[1] for r in rows]
        return "\n".join(out)

    def positions_text(self) -> str:
        b = self.book
        out = []
        for code, p in b.positions.items():
            px = self.closes.get(code, p["entry"])
            r = abs(p["entry"] - p["init_stop"]) * p["qty"] * b.mult(code)
            pnl = b.pnl(p, px)
            out.append(f"{label(code, p.get('name', ''))} {'多' if p['side'] == 'long' else '空'} {qty_text(code, p['qty'])} "
                       f"進場 {fmt(p['entry'])}（{tw(p['opened'])}）最近收盤 {fmt(px)} 停損 {fmt(p['stop'])} "
                       f"目標 {fmt(p.get('tp'))} 未實現 {pnl:+,.0f} 元（{pnl / r if r else 0:+.2f}R） 理由：{p['reason']}")
        for a in b.pending:
            out.append(f"（等下一個開盤）{'進場' if a['type'] == 'open' else '出場'} {label(a['code'], a.get('name', ''))} "
                       f"{a.get('side', '')} 停損 {fmt(a.get('stop'))} 目標 {fmt(a.get('tp'))}")
        return "\n".join(out) or "（目前沒有持倉）"

    def history_text(self, n: int = 6) -> str:
        lines = []
        if self.journal.exists():
            for raw in self.journal.read_text(encoding="utf-8").splitlines()[-n:]:
                d = json.loads(raw)
                acts = "; ".join(f"{a.get('type')} {a.get('code', '')} {a.get('result', '')}" for a in d.get("actions", []))
                lines.append(f"{d['time']} {d.get('trigger', '')} 看法：{str(d.get('view', ''))[:300]} | 動作：{acts or '觀望'}")
        tr = self.book.trades()
        lines.append("最近出場：" + ("; ".join(f"{t['code']} {t['name']} {t['side']} {t['pnl']}元 ({t['r']}R, {t['why']})"
                                         for t in tr[-10:]) or "無"))
        return "\n".join(lines)

    def plan_text(self, p: dict) -> str:
        how = "盤中碰到" if p["confirm"] == "touch" else "收盤"
        where = "站上" if p["when"] == "above" else "跌破"
        act = ("做多" if p["side"] == "long" else "做空") if p["code"] == FUT else "買進"
        then = "就" + act if p["confirm"] == "touch" else f"，下個開盤{act}"
        tp = f"，目標 {fmt(p['tp'])}" if p.get("tp") else ""
        return (f"{label(p['code'], p.get('name', ''))}：{how}{where} {fmt(p['trigger'])}{then}，停損 {fmt(p['stop'])}"
                f"{tp}，風險 {p['risk_pct']:g}%，有效到 {md(p['expires'])}")

    def build_prompt(self, g: dict, kind: str, live: str = "", focus: list[str] | None = None) -> str:
        c, b = self.cfg, self.book
        eq = b.equity(self.closes)
        news = "\n".join(f"{tw(i['ts'])} [{i['src']}] {i['title']}" for i in g["news"][:90]) or "（暫時抓不到新聞）"
        flagged = [f"{u['code']} {u['name']}：{'、'.join(s['flags'])}" for u, s in g["rows"] if s["flags"]]
        last = md(g["days"][-1]) if g["days"] else "?"
        extra = ""
        if kind == "即時":
            extra += ("\n這是使用者在 Telegram 用 /now 叫你做的即時判斷。"
                      + (f"現在是盤中：open／close 會以【盤中即時】的現價立即成交（不是等開盤），停損距離也以現價計算；"
                         f"掃描表和籌碼是 {last} 收盤的資料，今天的法人、融資要收盤後才知道。\n\n【盤中即時】\n{live}\n"
                         if live else "現在不是盤中：open／close 照常在下一個開盤成交。\n"))
        if focus:
            names = "、".join(label(x, self.names.get(x, "")) for x in focus)
            extra += (f"\n使用者特別想問：{names}。請一定要對這幾檔給明確看法（買、等、不碰，價位和理由）；"
                      f"不在前 {c['universe']} 大的也可以用 twmarket.py 查，但只有前 {c['universe']} 大和 TX 能下 open／plan。\n")
        return f"""現在時間 {self.now():%Y-%m-%d %H:%M}（台北時間）。這次是【{kind}】決策。最近一個已收盤的交易日：{last}。{extra}

【虛擬帳（建議追蹤）】權益 {eq:,.0f} 元（起始 {b.capital:,.0f} 元，{(eq / b.capital - 1) * 100:+.1f}%）
【硬性限制（程式強制）】個股只做多（現股，不融資）、台指期 {FUT} 可多可空；每筆風險 ≤ {c['max_risk_pct']:g}% 權益；全部倉位一起打到停損 ≤ {c['max_open_risk_pct']:g}%；最多 {c['max_positions']} 檔（含等開盤的）；一檔一倉不加碼；股票停損距離 {c['stock_stop_pct'][0]:g}–{c['stock_stop_pct'][1]:g}%、台指期 {c['future_stop_pct'][0]:g}–{c['future_stop_pct'][1]:g}%；停損只能收緊；單檔成本 ≤ {c['max_position_pct']:g}% 權益；進場在下一個開盤價成交，開盤跳空超過 {c['max_gap_pct']:g}% 或開盤漲停就不追。

【目前持倉】
{self.positions_text()}

【你掛著的條件單】
{chr(10).join(self.plan_text(p) + '（理由：' + p['reason'] + '）' for p in b.plans) or '（沒有）'}

【你最近的決策與出場】
{self.history_text()}

【台股大盤與籌碼】
{self.market_text(g)}

【國際盤】
{self.global_text(g)}

【掃描：市值前 {c['universe']} 大上市股＋前 {c['otc']} 大上櫃股（產業欄標「(櫃)」）＋成交值前 {c['etfs']} 大股票型 ETF，共 {len(g['rows'])} 檔（{last} 收盤）】
{SCAN_HEADER}
{chr(10).join(scan_line(u, s) for u, s in g['rows'])}

【程式篩選出的訊號（只是提醒多看一眼，不是建議）】
{chr(10).join(flagged) or '（沒有）'}

【最近 36 小時新聞（新的在上）】
{news}
"""

    def write_work(self, g: dict) -> None:
        """每檔一個小檔（work/twstock/chips/2330.json）：整包太大，AI 的 Read 一次讀不下。"""
        try:
            d = self.work_dir / "chips"
            d.mkdir(parents=True, exist_ok=True)
            for code, info in g["detail"].items():
                f = d / f"{code}.json"
                f.write_text(json.dumps({"code": code, **info}, ensure_ascii=False, indent=1), encoding="utf-8")
                f.chmod(0o644)
            for p in (self.work_dir, d):
                p.chmod(0o755)
        except OSError as exc:
            log.warning("work dir: %s", exc)

    def refresh_scan(self) -> None:
        """不呼叫 AI，只把最新的掃描資料整理好給問答用。"""
        g = self.gather()
        self.write_work(g)
        self.write_work_text("last_scan.txt", self.build_prompt(g, "資料整理（不是決策）"))
        if g["days"]:
            self.set_flag("scan_day", g["days"][-1])

    def scan_fresh(self) -> bool:
        if not (self.work_dir / "last_scan.txt").exists():
            return False
        return self.flags.get("scan_day") == self.data.last_days(self.now(), 1)[-1]

    def write_work_text(self, name: str, text: str) -> None:
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            f = self.work_dir / name
            f.write_text(text, encoding="utf-8")
            f.chmod(0o644)
        except OSError as exc:
            log.warning("work dir: %s", exc)

    # -- deciding --
    def decide(self, kind: str, focus: list[str] | None = None) -> None:
        try:
            self._decide(kind, focus)
        finally:
            self.live_now = False

    def _decide(self, kind: str, focus: list[str] | None) -> None:
        try:
            g = self.gather()
            self.live_now = kind == "即時" and self.in_session()
            live = self.live_text(g) if self.live_now else ""
        except Exception as exc:
            log.exception("gather failed")
            self.notify(f"⚠️ {kind}資料抓取失敗：{str(exc)[:200]}")
            return
        self.write_work(g)
        prompt = self.build_prompt(g, kind, live, focus)
        self.write_work_text("last_scan.txt", prompt)  # 問答時 AI 讀這份
        if g["days"]:
            self.set_flag("scan_day", g["days"][-1])
        try:
            decision, meta = self.ai.decide(prompt)
        except Exception as exc:
            log.error("decision failed: %s", exc)
            self._journal({"view": f"決策失敗：{str(exc)[:200]}", "actions": []}, kind, {})
            self.notify(f"⚠️ {kind}決策失敗：{str(exc)[:200]}")
            return
        acts = [a for a in decision.get("actions", [])[:12] if isinstance(a, dict)]
        results = [self.apply(a) for a in acts]
        decision["actions"] = results
        self._journal(decision, kind, meta)
        icon = {"盤前": "🌅 盤前", "盤後": "🌆 盤後", "即時": "⚡ 即時" + ("（盤中）" if self.live_now else "")}.get(kind, f"🧪 {kind}")
        acted = [a for a in results if a.get("ok")]
        rejected = [a for a in results if not a.get("ok")]
        msg = [icon + ("" if acted or rejected else "｜👀 觀望"), str(decision.get("view", "")).strip()[:2500]]
        msg += [a["msg"] for a in acted]
        msg += [f"⛔ 不採用：{ACTION_NAMES.get(a.get('type'), a.get('type'))} {a.get('code', '')} — {a['result']}"
                for a in rejected]
        if decision.get("watch"):
            msg.append("等待：\n" + str(decision["watch"]).strip()[:1200])
        msg.append(self.summary())
        self.say("\n\n".join(m for m in msg if m))

    def _journal(self, decision: dict, trigger: str, meta: dict) -> None:
        row = {"time": f"{self.now():%Y-%m-%d %H:%M}", "trigger": trigger, **decision,
               "cost_usd": meta.get("total_cost_usd"), "turns": meta.get("num_turns")}
        with self.journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def summary(self) -> str:
        b = self.book
        eq = b.equity(self.closes)
        tr = b.trades()
        wins = sum(1 for t in tr if float(t["pnl"]) > 0)
        rs = sum(float(t["r"]) for t in tr)
        return (f"📒 建議追蹤：權益 {eq:,.0f}（{(eq / b.capital - 1) * 100:+.1f}%），持倉 {len(b.positions)}、"
                f"等開盤 {len(b.pending)}、條件單 {len(b.plans)}；已出場 {len(tr)} 筆（賺 {wins}，合計 {rs:+.1f}R）")

    # -- actions --
    def check_entry(self, a: dict, ref: float | None, new: bool = True) -> str | None:
        """None = 可以；否則是拒絕原因。new=False：條件單觸發時（代號在掛單時已經檢查過）。"""
        c, b = self.cfg, self.book
        code, side = a["code"], a["side"]
        if new and code != FUT and code not in self.names:
            return f"{code} 不在掃描範圍（上市前 {c['universe']}、上櫃前 {c['otc']}、ETF 前 {c['etfs']}）"
        if code != FUT and side != "long":
            return "個股只做多（大盤看空請用台指期 TX）"
        if side not in ("long", "short"):
            return "side 要是 long/short"
        if code in b.positions or any(p["code"] == code for p in b.pending):
            return "已經有這檔的倉位或等開盤的建議（不加碼）"
        if len(b.positions) + sum(p["type"] == "open" for p in b.pending) >= c["max_positions"]:
            return f"已經有 {c['max_positions']} 檔"
        if not ref:
            return "沒有參考價"
        lo, hi = c["future_stop_pct"] if code == FUT else c["stock_stop_pct"]
        dist = (ref - a["stop"]) / ref * 100 * (1 if side == "long" else -1)
        if dist <= 0:
            return f"價格 {fmt(ref)} 已經在停損 {fmt(a['stop'])} 外面"
        # 條件單觸發時（new=False）最小距離不再卡：掛單時檢查過了，成交在更好的價位停損本來就會變近
        if dist > hi or (new and dist < lo):
            return f"停損距離參考價 {fmt(ref)} 是 {dist:.1f}%，不在 {lo:g}–{hi:g}%"
        tp = a.get("tp")
        if tp is not None and (tp - ref) * (1 if side == "long" else -1) <= 0:
            return "目標價在錯的一邊"
        eq = b.equity(self.closes)
        open_risk = sum(b.risk_at_stop(p) for p in b.positions.values())
        open_risk += sum(eq * p["risk_pct"] / 100 for p in b.pending if p["type"] == "open")
        if open_risk + eq * a["risk_pct"] / 100 > eq * c["max_open_risk_pct"] / 100 + 1e-6:
            return f"全部倉位一起打到停損會超過 {c['max_open_risk_pct']:g}%"
        return None

    def parse_entry(self, a: dict) -> dict:
        code = str(a.get("code", a.get("coin", ""))).upper().strip()
        code = FUT if code in ("TX", "TXF", "MTX", "TMF", "台指期") else code
        tp = num(a.get("take_profit"))
        return {"code": code, "name": self.names.get(code, ""), "side": a.get("side", "long"),
                "stop": num(a.get("stop")), "tp": tp,
                "risk_pct": min(max(num(a.get("risk_pct")) or 1.0, 0.1), self.cfg["max_risk_pct"]),
                "reason": str(a.get("reason", ""))[:300]}

    def apply(self, a: dict) -> dict:
        kind = a.get("type")
        e = self.parse_entry(a)
        code, b = e["code"], self.book
        out = {"type": kind, "code": code, "reason": e["reason"]}
        name = label(code, e["name"])
        if kind == "open":
            if e["stop"] is None:
                return {**out, "ok": False, "result": "沒有停損"}
            ref = self.closes.get(code)
            why = self.check_entry(e, ref)
            if why:
                return {**out, "ok": False, "result": why}
            act = ("做多" if e["side"] == "long" else "做空") if code == FUT else "買進"
            if self.live_now:  # 盤中即時決策：現價成交
                try:
                    p = b.open(e, ref, self.clock(), b.equity(self.closes), intraday=True)
                except ValueError as exc:
                    return {**out, "ok": False, "result": str(exc)}
                return {**out, "ok": True, "result": "現價進場",
                        "msg": f"✅ {name}：現價 {fmt(ref)} {act} {qty_text(code, p['qty'])}{p['note']}\n"
                               f"停損 {fmt(p['stop'])}（{abs(ref - p['stop']) / ref * 100:.1f}%），目標 {fmt(p.get('tp'))}，"
                               f"打到停損約虧 {b.risk_at_stop(p):,.0f} 元\n理由：{e['reason']}"}
            qty, note = b.size(code, e["side"], ref, e["stop"], e["risk_pct"], b.equity(self.closes))
            b.pending.append({"type": "open", **e, "ref": ref, "created": self.clock()})
            b.save()
            loss = b.equity(self.closes) * e["risk_pct"] / 100
            return {**out, "ok": True, "result": "等開盤",
                    "msg": f"🛒 {name}：下個開盤{act}（參考 {fmt(ref)}，跳空超過 {self.cfg['max_gap_pct']:g}% 不追），"
                           f"停損 {fmt(e['stop'])}（{abs(ref - e['stop']) / ref * 100:.1f}%），目標 {fmt(e['tp'])}\n"
                           f"約 {qty_text(code, qty)}{note}，打到停損約虧 {loss:,.0f} 元（權益 {e['risk_pct']:g}%）\n理由：{e['reason']}"}
        if kind == "close":
            if code not in b.positions:
                return {**out, "ok": False, "result": "沒有這檔持倉"}
            if self.live_now and self.closes.get(code):
                row = b.close(code, self.closes[code], "建議出場（盤中現價）", self.clock())
                return {**out, "ok": True, "result": "現價出場",
                        "msg": f"📤 {name}：現價 {fmt(row['exit'])} 出場，{fmt(row['entry'])} → {fmt(row['exit'])}，"
                               f"損益 {row['pnl']:+,} 元（{row['r']:+.2f}R）\n理由：{e['reason']}"}
            b.pending =[p for p in b.pending if p["code"] != code] + [{"type": "close", "code": code, "name": e["name"],
                                                                        "reason": e["reason"], "created": self.clock()}]
            b.save()
            return {**out, "ok": True, "result": "等開盤出場",
                    "msg": f"📤 {name}：下個開盤出場\n理由：{e['reason']}"}
        if kind == "move_stop":
            p = b.positions.get(code)
            if not p or e["stop"] is None:
                return {**out, "ok": False, "result": "沒有這檔持倉或沒給停損"}
            sgn = 1 if p["side"] == "long" else -1
            ref = self.closes.get(code, p["entry"])
            if (e["stop"] - p["stop"]) * sgn <= 0:
                return {**out, "ok": False, "result": f"停損只能收緊（目前 {fmt(p['stop'])}）"}
            if (ref - e["stop"]) * sgn <= 0:
                return {**out, "ok": False, "result": f"新停損已經越過最近收盤 {fmt(ref)}"}
            old, p["stop"] = p["stop"], e["stop"]
            b.save()
            return {**out, "ok": True, "result": "移動停損",
                    "msg": f"🔒 {name}：停損 {fmt(old)} → {fmt(e['stop'])}\n理由：{e['reason']}"}
        if kind == "take_profit":
            p = b.positions.get(code)
            if not p:
                return {**out, "ok": False, "result": "沒有這檔持倉"}
            p["tp"] = e["tp"]
            b.save()
            return {**out, "ok": True, "result": "改目標", "msg": f"🎯 {name}：目標改為 {fmt(e['tp'])}\n理由：{e['reason']}"}
        if kind == "plan":
            return self.add_plan(a, e, out)
        if kind == "cancel_plan":
            if not any(p["code"] == code for p in b.plans):
                return {**out, "ok": False, "result": "沒有這張條件單"}
            b.plans = [p for p in b.plans if p["code"] != code]
            b.save()
            return {**out, "ok": True, "result": "取消條件單", "msg": f"🗑 取消 {name} 條件單\n理由：{e['reason']}"}
        return {**out, "ok": False, "result": f"不認得的動作 {kind}"}

    def add_plan(self, a: dict, e: dict, out: dict) -> dict:
        c, b = self.cfg, self.book
        trigger = num(a.get("trigger"))
        if trigger is None or e["stop"] is None:
            return {**out, "ok": False, "result": "條件單要有 trigger 和 stop"}
        ref = self.closes.get(e["code"])
        why = self.check_entry(e, trigger)
        if why:
            return {**out, "ok": False, "result": why.replace("參考價", "觸發價")}
        when = a.get("when") or ("above" if ref and trigger > ref else "below")
        confirm = a.get("confirm") or "close"
        if when not in ("above", "below") or confirm not in ("touch", "close"):
            return {**out, "ok": False, "result": "when 要是 above/below，confirm 要是 touch/close"}
        if ref and (when == "above") != (trigger > ref):
            return {**out, "ok": False, "result": f"最近收盤 {fmt(ref)} 已經在觸發價{'上' if when == 'above' else '下'}方，直接用 open"}
        days = int(min(max(num(a.get("expires_days")) or 5, 1), c["plan_max_days"]))
        expires = self.data.add_trading_days(self.now().date(), days).strftime("%Y%m%d")
        plan = {**e, "trigger": trigger, "when": when, "confirm": confirm, "expires": expires, "created": self.clock()}
        others = [p for p in b.plans if p["code"] != e["code"]]
        if len(others) >= c["max_plans"]:
            return {**out, "ok": False, "result": f"已經有 {c['max_plans']} 張條件單"}
        replaced = len(others) != len(b.plans)
        b.plans = others + [plan]
        b.save()
        return {**out, "ok": True, "result": "條件單",
                "msg": f"📌 {'更新' if replaced else '掛'}條件單 {self.plan_text(plan)}\n理由：{e['reason']}"}

    # -- during the session --
    def quotes(self) -> dict[str, dict]:
        b = self.book
        codes = {p["code"] for p in b.pending} | set(b.positions) | {p["code"] for p in b.plans}
        out = {}
        stocks = sorted(c for c in codes if c != FUT)
        if stocks:
            try:
                out.update(self.data.quotes(stocks))
            except Exception as exc:
                log.warning("quotes: %s", exc)
        if FUT in codes:
            try:
                q = self.data.tx_quote()
                if q:
                    out[FUT] = q
            except Exception as exc:
                log.warning("tx quote: %s", exc)
        return out

    def fill_pending(self, q: dict[str, dict], today: str) -> None:
        b, c = self.book, self.cfg
        keep = []
        for a in b.pending:
            qt = q.get(a["code"])
            if not qt or qt.get("date") != today or not qt.get("o"):
                keep.append(a)  # 還沒開盤（或還沒有成交）
                continue
            o, name = qt["o"], label(a["code"], a.get("name", ""))
            if a["type"] == "close":
                if a["code"] in b.positions:
                    self._closed(b.close(a["code"], o, "建議出場（開盤價）", self.clock()))
                continue
            sgn = 1 if a["side"] == "long" else -1
            gap = (o / a["ref"] - 1) * 100 * sgn
            skip = None
            if gap > c["max_gap_pct"]:
                skip = f"開盤 {fmt(o)} 跳空 {gap:+.1f}%，超過 {c['max_gap_pct']:g}%，不追"
            elif (o - a["stop"]) * sgn <= 0:
                skip = f"開盤 {fmt(o)} 已經在停損 {fmt(a['stop'])} 外面"
            elif a["code"] != FUT and qt.get("u") and o >= qt["u"]:
                skip = f"開盤漲停 {fmt(o)}，買不到"
            if skip:
                self.notify(f"⏭ {name} 沒有進場：{skip}")
                continue
            try:
                p = b.open(a, o, self.clock(), b.equity(self.prices(q)))
            except ValueError as exc:
                self.notify(f"⏭ {name} 沒有進場：{exc}")
                continue
            act = ("做多" if a["side"] == "long" else "做空") if a["code"] == FUT else "買進"
            self.notify(f"✅ {name} 開盤 {fmt(o)} {act} {qty_text(a['code'], p['qty'])}{p['note']}\n"
                        f"停損 {fmt(p['stop'])}，目標 {fmt(p.get('tp'))}，打到停損約虧 {b.risk_at_stop(p):,.0f} 元")
        b.pending = keep
        b.save()

    def prices(self, q: dict[str, dict]) -> dict[str, float]:
        return {**self.closes, **{k: v["z"] for k, v in q.items() if v.get("z")}}

    def check_exits(self, q: dict[str, dict], today: str) -> None:
        b = self.book
        for code, p in list(b.positions.items()):
            qt = q.get(code)
            if not qt or qt.get("date") != today or not qt.get("o"):
                continue
            sgn = 1 if p["side"] == "long" else -1
            opened_today = datetime.fromtimestamp(p["opened"], TZ).strftime("%Y%m%d") == today
            # 今天盤中才進場的倉位，今天稍早的高低點不算（只看最新價）
            touch_entry = opened_today and p.get("intraday")
            lo = qt["z"] if touch_entry else (qt["l"] if sgn > 0 else qt["h"])
            hi = qt["z"] if touch_entry else (qt["h"] if sgn > 0 else qt["l"])
            if lo is None or hi is None:
                continue
            gap_open = not opened_today
            if (lo - p["stop"]) * sgn <= 0:
                px = qt["o"] if gap_open and (qt["o"] - p["stop"]) * sgn <= 0 else p["stop"]
                self._closed(b.close(code, px, "停損", self.clock()))
            elif p.get("tp") and (hi - p["tp"]) * sgn >= 0:
                px = qt["o"] if gap_open and (qt["o"] - p["tp"]) * sgn >= 0 else p["tp"]
                self._closed(b.close(code, px, "到目標", self.clock()))

    def check_touch_plans(self, q: dict[str, dict], today: str) -> None:
        b, c = self.book, self.cfg
        keep = []
        for p in b.plans:
            qt = q.get(p["code"])
            if p["confirm"] != "touch" or not qt or qt.get("date") != today or not qt.get("o"):
                keep.append(p)
                continue
            above = p["when"] == "above"
            # 今天盤中才掛的單：今天稍早（掛單前）的高低點和開盤價都不算，只看掛單後每次檢查看到的價格
            opened_at = datetime.fromtimestamp(p["created"], TZ)
            fresh = opened_at.strftime("%Y%m%d") == today and opened_at.strftime("%H:%M") >= "09:00"
            if fresh:
                if not qt.get("z"):
                    keep.append(p)
                    continue
                p["lo"] = min(p.get("lo") or qt["z"], qt["z"])
                p["hi"] = max(p.get("hi") or qt["z"], qt["z"])
                hit = p["hi"] >= p["trigger"] if above else p["lo"] <= p["trigger"]
                gapped = False
            else:
                hit = (qt["h"] or 0) >= p["trigger"] if above else (qt["l"] or 1e18) <= p["trigger"]
                gapped = (qt["o"] >= p["trigger"]) if above else (qt["o"] <= p["trigger"])
            if not hit:
                keep.append(p)
                continue
            entry = qt["o"] if gapped else p["trigger"]
            name = label(p["code"], p.get("name", ""))
            why = self.check_entry(p, entry, new=False)
            sgn = 1 if p["side"] == "long" else -1
            if not why and gapped and abs(entry / p["trigger"] - 1) * 100 > c["max_gap_pct"]:
                why = f"開盤 {fmt(entry)} 直接跳過觸發價 {abs(entry / p['trigger'] - 1) * 100:.1f}%，不追"
            pos = None
            if not why:
                try:
                    pos = b.open(p, entry, self.clock(), b.equity(self.prices(q)), intraday=not gapped)
                except ValueError as exc:
                    why = str(exc)
            if not pos:
                self.notify(f"⚡ {name} 條件單觸發，但沒有進場\n原因：{why}\n（條件：{self.plan_text(p)}）")
                continue
            act = ("做多" if p["side"] == "long" else "做空") if p["code"] == FUT else "買進"
            self.notify(f"⚡ 條件單觸發 → ✅ {name} {fmt(entry)} {act} {qty_text(p['code'], pos['qty'])}{pos['note']}\n"
                        f"停損 {fmt(pos['stop'])}，目標 {fmt(pos.get('tp'))}，打到停損約虧 {b.risk_at_stop(pos):,.0f} 元\n"
                        f"理由：{p['reason']}")
        b.plans = keep
        b.save()

    def after_close(self, today: str) -> None:
        """收盤確認的條件單：收盤站上／跌破就排到下個開盤進場；過期的條件單移除。"""
        b = self.book
        q = self.quotes()
        self.check_exits(q, today)
        keep = []
        for p in b.plans:
            name = label(p["code"], p.get("name", ""))
            qt = q.get(p["code"])
            if p["confirm"] == "close" and qt and qt.get("date") == today and qt.get("z"):
                close = qt["z"]
                if (close > p["trigger"]) if p["when"] == "above" else (close < p["trigger"]):
                    why = self.check_entry(p, close, new=False)
                    if why:
                        self.notify(f"⚡ {name} 收盤 {fmt(close)} 觸發條件單，但沒有排進場\n原因：{why}\n"
                                    f"（條件：{self.plan_text(p)}）")
                    else:
                        b.pending.append({"type": "open", **{k: p[k] for k in ("code", "name", "side", "stop", "tp",
                                                                                 "risk_pct", "reason")},
                                          "ref": close, "created": self.clock()})
                        act = ("做多" if p["side"] == "long" else "做空") if p["code"] == FUT else "買進"
                        self.notify(f"⚡ 條件單觸發：{name} 收盤 {fmt(close)}"
                                    f"{'站上' if p['when'] == 'above' else '跌破'} {fmt(p['trigger'])}\n"
                                    f"→ 下個開盤{act}（跳空超過 {self.cfg['max_gap_pct']:g}% 不追），停損 {fmt(p['stop'])}")
                    continue
            if p["expires"] <= today:
                self.notify(f"⌛ 條件單到期：{self.plan_text(p)}")
                continue
            keep.append(p)
        b.plans = keep
        b.save()

    def _closed(self, row: dict) -> None:
        icon = "🎯" if float(row["pnl"]) > 0 else "🛑"
        self.notify(f"{icon} {label(row['code'], row['name'])} 出場（{row['why']}）\n"
                    f"{fmt(row['entry'])} → {fmt(row['exit'])}，損益 {row['pnl']:+,} 元（{row['r']:+.2f}R）\n{self.summary()}")

    # -- main loop --
    def step(self) -> None:
        now = self.now()
        today, hm = now.strftime("%Y%m%d"), now.strftime("%H:%M")
        if not self.data.trading_day(now.date()):
            return
        c = self.cfg
        if c["premarket"] <= hm < "09:00" and self.flags.get("pre") != today:
            self.set_flag("pre", today)
            self.decide("盤前")
        if "09:00" <= hm <= "13:46" and self.clock() - self.last_check >= c["check_minutes"] * 60:
            self.last_check = self.clock()
            b = self.book
            if b.pending or b.positions or any(p["confirm"] == "touch" for p in b.plans):
                q = self.quotes()
                self.fill_pending(q, today)
                self.check_exits(q, today)
                self.check_touch_plans(q, today)
        if "13:50" <= hm and self.flags.get("close") != today:
            self.set_flag("close", today)
            self.after_close(today)
        if hm >= c["postmarket"] and self.flags.get("post") != today:
            self.set_flag("post", today)
            self.decide("盤後")

    HELP = ("指令：\n/now　用現在的行情判斷一次（盤中會用即時報價，建議以現價成交）\n"
            "/now 2330 2454　特別問這幾檔\n直接打字　問 AI 任何台股問題\n"
            "/new　開新對話（忘掉前面問過的）\n/tw　建議追蹤的持倉、條件單、戰績\n/help　這份說明")

    def say(self, text: str, limit: int = 3800) -> None:
        """Telegram 一則最多 4096 字：太長就在換行處切成幾則。"""
        while len(text) > limit:
            cut = text.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            self.notify(text[:cut])
            text = text[cut:].lstrip("\n")
        if text:
            self.notify(text)

    def ask_prompt(self, question: str) -> str:
        last = {}
        if self.journal.exists():
            rows = self.journal.read_text(encoding="utf-8").splitlines()
            last = json.loads(rows[-1]) if rows else {}
        work = self.work_dir
        return f"""使用者在台股聊天室問你（{self.now():%Y-%m-%d %H:%M} 台北時間{'，現在盤中' if self.in_session() else ''}）：
{question}

這是問答，不是決策：用一般繁體中文回答，**不要輸出 JSON**，你的回答不會執行任何動作。使用者想讓你重新判斷、調整建議，請他傳 /now。
可以用的資料：
- 最近一次完整決策時的資料（掃描表、大盤籌碼、國際盤、新聞）：用 Read 讀 {work / 'last_scan.txt'}（很長，用 offset/limit 或先找你要的段落）
- 個股最近 10 天每天的外資、投信、自營商、融資融券：{work / 'chips'}/代號.json（例如 {work / 'chips' / '2330.json'}）
- 即時報價、K 線：python3 /opt/cryptobot-ai/twmarket.py quote 2330 ／ klines 2330 --tf 1d --limit 120
回答要具體、適合手機閱讀（短段落；Telegram 不顯示 Markdown，不要用 ** 粗體、# 標題或表格，小標題直接寫成一行文字）：有價位就給價位，說清楚依據；資料沒有的就說沒有，不要編數字。

【建議追蹤現況】
{self.status()}

【最近一次決策】{last.get('time', '（還沒有）')} {last.get('trigger', '')}
{str(last.get('view', ''))[:2500]}
等待：{str(last.get('watch', '') or '（無）')[:800]}
"""

    def ask(self, question: str) -> None:
        if not self.chat:
            self.notify("⚠️ 問答功能沒有啟動（需要台股專用機器人）")
            return
        self.notify("💬 想一下…")
        if not self.scan_fresh():
            try:
                self.refresh_scan()
            except Exception as exc:
                log.warning("refresh scan: %s", exc)
        try:
            answer = self.chat.ask(self.ask_prompt(question[:2000]))
        except Exception as exc:
            log.exception("ask failed")
            answer = f"⚠️ AI 回覆失敗：{str(exc)[:200]}"
        self.say(answer)

    def handle(self, text: str) -> None:
        parts = text.strip().split()
        if not parts:
            return
        cmd = parts[0].split("@")[0].lower()
        if not cmd.startswith("/"):
            self.ask(text.strip())
        elif cmd == "/new":
            if self.chat:
                self.chat.session_id = None
            self.notify("🆕 已開新對話")
        elif cmd in ("/now", "/n"):
            wait = self.cfg["now_cooldown_seconds"] - (self.clock() - self.last_now)
            if wait > 0:
                self.notify(f"⏳ 上一次即時判斷才剛做完或還在跑，{wait / 60:.0f} 分鐘後再叫")
                return
            focus = []
            for a in parts[1:]:
                a = a.upper()
                if a in ("TX", "台指期", "台指"):
                    focus.append(FUT)
                elif re.fullmatch(r"\d{4,6}[A-Z]?", a):
                    focus.append(a)
            self.last_now = self.clock()
            self.notify("🔎 收到，AI 判斷中（約 3–8 分鐘）" + (f"，特別看 {' '.join(focus)}" if focus else ""))
            self.decide("即時", focus[:8])
            self.last_now = self.clock()
        elif cmd in ("/tw", "/status"):
            self.notify(self.status())
        elif cmd in ("/help", "/start"):
            self.notify(self.HELP)

    def status(self) -> str:
        b = self.book
        lines = [self.summary(), "", self.positions_text()]
        if b.plans:
            lines += ["", "條件單："] + [self.plan_text(p) for p in b.plans]
        return "\n".join(lines)

    def run(self) -> None:
        self.notify("📈 台股 AI 建議啟動：盤前 " + self.cfg["premarket"] + "、盤後 " + self.cfg["postmarket"]
                    + "，盤中只在進場、出場、條件單觸發時通知"
                    + ("\n隨時傳 /now 叫 AI 判斷一次，或直接打字問問題（/help 看指令）" if self.commands else "") + "\n" + self.summary())
        if self.chat and not self.scan_fresh():
            try:  # 問答一開始就有完整資料可讀
                self.refresh_scan()
            except Exception as exc:
                log.warning("refresh scan: %s", exc)
        while True:
            try:
                self.step()
            except Exception as exc:
                log.exception("step failed: %s", exc)
            if not self.commands:
                time.sleep(60)
                continue
            try:
                for text in self.commands.poll(50):  # long poll：有指令就馬上回來，沒有最多等 50 秒
                    self.handle(text)
            except Exception as exc:
                log.warning("commands: %s", exc)
                time.sleep(30)


class Commands:
    """台股專用機器人的指令（getUpdates）。只能用在台股自己的機器人：主機器人的訊息歸管家讀。
    只接受設定的那個聊天室，其他人傳的都忽略。"""

    def __init__(self, token: str, chat_id: str, fetch=_get):
        self.url = f"https://api.telegram.org/bot{token}/getUpdates"
        self.chat_id, self.fetch = str(chat_id), fetch
        self.offset = None

    def _updates(self, timeout: int, offset) -> list[dict]:
        q = f"?timeout={timeout}" + (f"&offset={offset}" if offset is not None else "")
        return json.loads(self.fetch(self.url + q, timeout=timeout + 15)).get("result", [])

    def poll(self, timeout: int = 50) -> list[str]:
        if self.offset is None:  # 啟動前就在排隊的舊指令不執行
            old = self._updates(0, -1)
            self.offset = old[-1]["update_id"] + 1 if old else 0
        out = []
        for u in self._updates(timeout, self.offset):
            self.offset = u["update_id"] + 1
            m = u.get("message") or u.get("channel_post") or {}  # 頻道的貼文是 channel_post
            if str((m.get("chat") or {}).get("id")) == self.chat_id and m.get("text"):
                out.append(m["text"])
        return out


def status_text(cfg: dict) -> str:
    """Telegram /tw：建議追蹤的持倉、條件單、戰績。"""
    c = settings(cfg)
    state, logs = Path(cfg.get("state_dir", "state")), Path(cfg.get("log_dir", "logs"))
    b = Book(state / "twstock_book.json", logs / "twstock_trades.csv", c)
    lines = [f"📈 台股建議追蹤（虛擬 {b.capital:,.0f} 元）"]
    for code, p in b.positions.items():
        lines.append(f"{label(code, p.get('name', ''))} {'多' if p['side'] == 'long' else '空'} {qty_text(code, p['qty'])} "
                     f"@ {fmt(p['entry'])}｜停損 {fmt(p['stop'])}｜目標 {fmt(p.get('tp'))}")
    for a in b.pending:
        lines.append(f"等開盤{'進場' if a['type'] == 'open' else '出場'}：{label(a['code'], a.get('name', ''))}")
    tr = b.trades()
    if tr:
        lines.append(f"已出場 {len(tr)} 筆，賺 {sum(float(t['pnl']) > 0 for t in tr)} 筆，"
                     f"合計 {sum(float(t['pnl']) for t in tr):+,.0f} 元（{sum(float(t['r']) for t in tr):+.1f}R）")
    return "\n".join(lines)


def telegram_target() -> tuple[str | None, str | None]:
    """台股專用機器人（/etc/cryptobot-twstock/telegram.env）；沒設定就用主機器人。"""
    import os
    token = os.getenv("TWSTOCK_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TWSTOCK_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    return token, chat


MENU = [("now", "AI 用現在的行情判斷一次（可加代號：/now 2330）"),
        ("tw", "建議追蹤：持倉、條件單、戰績"),
        ("new", "開新對話"),
        ("help", "指令說明")]


def set_menu(token: str, fetch=_get) -> bool:
    """Telegram 輸入框旁的「選單」：打 / 就列出指令。"""
    body = json.dumps({"commands": [{"command": c, "description": d} for c, d in MENU]}).encode()
    try:
        r = json.loads(fetch(f"https://api.telegram.org/bot{token}/setMyCommands", data=body,
                             headers={"Content-Type": "application/json"}))
        return bool(r.get("ok"))
    except Exception as exc:
        log.warning("set menu: %s", exc)
        return False


def find_chat() -> int:
    """列出台股機器人最近收到訊息的聊天室（先在聊天室裡對機器人說句話）。"""
    import os
    token = os.getenv("TWSTOCK_TELEGRAM_BOT_TOKEN")
    if not token:
        print("TWSTOCK_TELEGRAM_BOT_TOKEN missing (/etc/cryptobot-twstock/telegram.env)")
        return 1
    d = json.loads(_get(f"https://api.telegram.org/bot{token}/getUpdates"))
    chats = {}
    for u in d.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        ch = m.get("chat") or {}
        if ch.get("id"):
            chats[ch["id"]] = ch.get("title") or ch.get("username") or ch.get("first_name") or ""
    if not chats:
        print("沒有看到任何聊天室：把機器人加進聊天室，在裡面傳一句話（群組可傳 /start@機器人名稱），再跑一次")
        return 1
    for cid, name in chats.items():
        print(f"{cid}\t{name}")
    return 0


def main(cfg: dict, repo_dir: str, once: bool = False, scan: bool = False, chat: bool = False) -> int:
    import os

    from .notify import Telegram

    if chat:
        return find_chat()
    c = settings(cfg)
    state = Path(cfg.get("state_dir", "state"))
    data = Data(state / "twstock")
    if scan:
        adv = Advisor(cfg, data, ai=None, work_dir=Path(os.getenv("TWSTOCK_WORK", str(WORK))))
        g = adv.gather()
        print(adv.build_prompt(g, "測試"))
        return 0
    token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    claude = Path(AI_ROOT) / "home" / ".local" / "bin" / "claude"
    if not token or not claude.exists():
        print("CLAUDE_CODE_OAUTH_TOKEN or the claude binary is missing")
        return 1
    ai = TWStockAI(cfg, repo_dir, str(claude), token, c["model"], int(c["max_turns"]))
    ai.install()
    tg_token, chat_id = telegram_target()
    notify = Telegram(tg_token, chat_id, prefix="[台股AI] ") if tg_token and chat_id else None
    adv = Advisor(cfg, data, ai, notify)
    own = os.getenv("TWSTOCK_TELEGRAM_BOT_TOKEN")
    if own and chat_id:  # 只聽台股自己的機器人，絕不搶管家（主機器人）的訊息
        adv.commands = Commands(own, chat_id)
        set_menu(own)
        adv.chat = TWChat(cfg, repo_dir, str(claude), token, c["model"], int(c["max_turns"]))
    if once:
        adv.decide("手動測試")
        print(adv.journal.read_text(encoding="utf-8").splitlines()[-1])
        return 0
    adv.run()
    return 0
