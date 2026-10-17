"""Public Binance USDT-perpetual market data for the butler's AI (no API key, read-only).

    python3 /opt/cryptobot-ai/market.py klines BTC --tf 4h --limit 100
    python3 /opt/cryptobot-ai/market.py funding ETH --limit 10
    python3 /opt/cryptobot-ai/market.py oi SOL --tf 1h --limit 24
    python3 /opt/cryptobot-ai/market.py ticker BTC ETH SOL

Runs as the unprivileged AI user; it never sees an API key, so it can only read public data.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import ccxt

TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d", "1w")
OI_TIMEFRAMES = ("5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d")


def coin(value: str) -> str:
    c = value.upper().split("/")[0].removesuffix("USDT")
    if not re.fullmatch(r"[A-Z0-9]{1,20}", c):
        raise argparse.ArgumentTypeError(f"bad coin: {value}")
    return f"{c}/USDT:USDT"


TW = timezone(timedelta(hours=8))  # the user is in Taiwan: all times are UTC+8


def ts(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, TW).strftime("%Y-%m-%d %H:%M")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("klines", help="OHLCV candles (Taipei time, UTC+8)")
    k.add_argument("coin", type=coin)
    k.add_argument("--tf", choices=TIMEFRAMES, default="4h")
    k.add_argument("--limit", type=int, default=100)
    f = sub.add_parser("funding", help="current and recent funding rates")
    f.add_argument("coin", type=coin)
    f.add_argument("--limit", type=int, default=10)
    o = sub.add_parser("oi", help="open interest history")
    o.add_argument("coin", type=coin)
    o.add_argument("--tf", choices=OI_TIMEFRAMES, default="1h")
    o.add_argument("--limit", type=int, default=24)
    t = sub.add_parser("ticker", help="last price and 24h change")
    t.add_argument("coins", type=coin, nargs="+")
    a = p.parse_args(argv)
    limit = max(1, min(int(getattr(a, "limit", 1)), 500))

    # The Claude Code sandbox only lets traffic out through its proxy (…_PROXY=http://user:pw@localhost:port),
    # and "localhost" does not resolve inside the sandbox.
    for k, v in list(os.environ.items()):
        if k.lower().endswith("_proxy"):
            os.environ[k] = re.sub(r"(//|@)localhost:", r"\g<1>127.0.0.1:", v)
    ex = ccxt.binanceusdm({"enableRateLimit": True})
    if os.getenv("HTTPS_PROXY"):  # ccxt ignores the proxy environment variables
        ex.https_proxy = os.environ["HTTPS_PROXY"]
    if a.cmd == "klines":
        print("time_tw,open,high,low,close,volume")
        for t0, op, hi, lo, cl, vol in ex.fetch_ohlcv(a.coin, a.tf, limit=limit):
            print(f"{ts(t0)},{op:.6g},{hi:.6g},{lo:.6g},{cl:.6g},{vol:.6g}")
    elif a.cmd == "funding":
        now = ex.fetch_funding_rate(a.coin)
        print(f"current {now.get('fundingRate')} next {now.get('fundingDatetime')} mark {now.get('markPrice')}")
        print("time_tw,rate")
        for r in ex.fetch_funding_rate_history(a.coin, limit=limit):
            print(f"{ts(r['timestamp'])},{r['fundingRate']}")
    elif a.cmd == "oi":
        print("time_tw,open_interest_contracts,open_interest_usdt")
        for r in ex.fetch_open_interest_history(a.coin, a.tf, limit=limit):
            print(f"{ts(r['timestamp'])},{r.get('openInterestAmount')},{r.get('openInterestValue')}")
    else:
        for sym, tk in ex.fetch_tickers(a.coins).items():
            print(f"{sym.split('/')[0]} last {tk.get('last')} 24h {tk.get('percentage')}% "
                  f"high {tk.get('high')} low {tk.get('low')} quote_volume {tk.get('quoteVolume')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
