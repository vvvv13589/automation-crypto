"""Command line interface.

    python -m cryptobot backtest --synthetic
    python -m cryptobot backtest --days 90
    python -m cryptobot paper
    python -m cryptobot live --confirm-live
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .config import load_config, load_dotenv


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def cmd_backtest(cfg: dict, args) -> int:
    from . import data
    from .backtest import run_backtest

    if args.synthetic:
        candles = data.synthetic(bars=args.bars, timeframe=cfg["timeframe"], seed=args.seed)
        source = f"synthetic ({args.bars} bars, seed={args.seed})"
    elif args.csv:
        candles = data.load_csv(args.csv)
        source = args.csv
    else:
        ex = data.make_exchange(cfg)
        since = ex.milliseconds() - args.days * 86400 * 1000
        candles = data.fetch_history(ex, cfg["symbol"], cfg["timeframe"], since)
        source = f"{cfg['exchange']['id']} {cfg['symbol']} {cfg['timeframe']} last {args.days}d"
        if args.save:
            candles.reset_index().assign(
                timestamp=lambda d: d["timestamp"].astype("int64") // 10**6
            ).to_csv(args.save, index=False)
            print(f"saved candles to {args.save}")

    result = run_backtest(cfg, candles)
    print(f"\nBacktest: {source}")
    print(json.dumps(result.summary(), indent=2, ensure_ascii=False))
    if args.trades:
        for t in result.trades:
            print(f"{t.opened_at} -> {t.closed_at} [{t.regime:5}] {t.entry_price:.2f} -> "
                  f"{t.exit_price:.2f} pnl={t.pnl:+.2f} ({t.pnl_pct:+.2f}%) {t.reason}")
    return 0


def cmd_run(cfg: dict, args, live: bool) -> int:
    from .runner import Runner

    if live:
        if not args.confirm_live:
            print("Refusing to trade real money without --confirm-live.", file=sys.stderr)
            return 2
        cfg["mode"] = "live"
    Runner(cfg, live=live).run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cryptobot", description="Adaptive crypto trading bot")
    parser.add_argument("-c", "--config", help="path to config YAML (default: ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--symbol", help="override symbol, e.g. ETH/USDT")
    parser.add_argument("--timeframe", help="override timeframe, e.g. 1h")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="test the strategy on historical data")
    bt.add_argument("--days", type=int, default=90, help="days of exchange history to download")
    bt.add_argument("--csv", help="use candles from CSV (timestamp,open,high,low,close,volume)")
    bt.add_argument("--save", help="save downloaded candles to this CSV")
    bt.add_argument("--synthetic", action="store_true", help="offline synthetic market data")
    bt.add_argument("--bars", type=int, default=5000)
    bt.add_argument("--seed", type=int, default=42)
    bt.add_argument("--trades", action="store_true", help="print every trade")

    sub.add_parser("paper", help="real-time simulated trading with live market data")
    live = sub.add_parser("live", help="REAL trading with real funds")
    live.add_argument("--confirm-live", action="store_true", help="required: I accept the risk")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    load_dotenv()
    cfg = load_config(args.config)
    if args.symbol:
        cfg["symbol"] = args.symbol
    if args.timeframe:
        cfg["timeframe"] = args.timeframe

    if args.command == "backtest":
        return cmd_backtest(cfg, args)
    return cmd_run(cfg, args, live=args.command == "live")


if __name__ == "__main__":
    sys.exit(main())
