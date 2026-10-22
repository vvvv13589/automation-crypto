"""Public Taiwan / global market data for the tw-stock AI (no account, read-only).

    python3 /opt/cryptobot-ai/twmarket.py klines 2330 --tf 1d --limit 120
    python3 /opt/cryptobot-ai/twmarket.py klines ^SOX --tf 1d --limit 60
    python3 /opt/cryptobot-ai/twmarket.py quote 2330 2317 2454

Taiwan listed stocks are given by their code (2330); anything else is a Yahoo symbol (^SOX, NVDA, TWD=X).
"""

import argparse
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))  # the user is in Taiwan: all times are UTC+8
TFS = {"1d": "2y", "1wk": "5y", "60m": "3mo", "30m": "1mo", "15m": "1mo", "5m": "5d"}
UA = {"User-Agent": "Mozilla/5.0 (cryptobot tw-stock)"}


def symbol(value: str) -> str:
    v = value.strip().upper()
    if not re.fullmatch(r"[\^A-Z0-9=.\-]{1,20}", v):
        raise argparse.ArgumentTypeError(f"bad symbol: {value}")
    return f"{v}.TW" if re.fullmatch(r"\d{4,6}[A-Z]?", v) else v


def get(url: str) -> dict:
    # requests (not urllib): it trusts the sandbox proxy's CA via REQUESTS_CA_BUNDLE, like ccxt in market.py
    import requests
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.json()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("klines", help="OHLCV candles (Taipei time, UTC+8; prices not adjusted for dividends)")
    k.add_argument("symbol", type=symbol)
    k.add_argument("--tf", choices=list(TFS), default="1d")
    k.add_argument("--limit", type=int, default=120)
    q = sub.add_parser("quote", help="TWSE real-time quote (the close after 13:30)")
    q.add_argument("codes", nargs="+")
    a = p.parse_args(argv)

    # The Claude Code sandbox only lets traffic out through its proxy (…_PROXY=http://user:pw@localhost:port),
    # and "localhost" does not resolve inside the sandbox.
    for key, v in list(os.environ.items()):
        if key.lower().endswith("_proxy"):
            os.environ[key] = re.sub(r"(//|@)localhost:", r"\g<1>127.0.0.1:", v)

    if a.cmd == "klines":
        limit = max(1, min(a.limit, 500))
        tries = [a.symbol, a.symbol[:-3] + ".TWO"] if a.symbol.endswith(".TW") else [a.symbol]  # 上櫃是 .TWO
        r = None
        for sym in tries:
            try:
                d = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
                        f"?range={TFS[a.tf]}&interval={a.tf}")
                r = d["chart"]["result"][0]
                if r.get("timestamp"):
                    break
            except Exception:
                continue
        if not r:
            print(f"no data for {a.symbol}")
            return 1
        qt = r["indicators"]["quote"][0]
        tw = a.symbol.endswith(".TW")
        rows = []
        for i, t in enumerate(r.get("timestamp") or []):
            o, h, l, c, v = (qt[x][i] for x in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            when = datetime.fromtimestamp(t, TW).strftime("%Y-%m-%d" if a.tf in ("1d", "1wk") else "%Y-%m-%d %H:%M")
            vol = f"{(v or 0) / 1000:.0f}" if tw else f"{v or 0:.0f}"
            rows.append(f"{when},{o:.6g},{h:.6g},{l:.6g},{c:.6g},{vol}")
        print("time_tw,open,high,low,close," + ("volume_lots(張)" if tw else "volume"))
        print("\n".join(rows[-limit:]))
    else:
        codes = [c.strip().upper() for c in a.codes if re.fullmatch(r"\d{4,6}[A-Za-z]?", c.strip())][:25]
        if not codes:
            print("give Taiwan stock codes, e.g. 2330")
            return 1
        d = get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch="  # 上市 tse_、上櫃 otc_ 都問
                + "|".join(f"{ex}_{c}.tw" for c in codes for ex in ("tse", "otc")))
        print("code,name,date,time,last,open,high,low,prev_close,limit_up,limit_down,volume_lots")
        for m in d.get("msgArray", []):
            if not m.get("c") or not m.get("n"):
                continue  # 這個代號不在這個市場（上市／上櫃只會有一邊）
            last = m.get("z") if m.get("z") not in (None, "-") else (m.get("trade") or {}).get("z", "-")
            print(",".join(str(x) for x in (m.get("c"), m.get("n"), m.get("d"), m.get("t"), last, m.get("o"), m.get("h"),
                                            m.get("l"), m.get("y"), m.get("u"), m.get("w"), m.get("v"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
