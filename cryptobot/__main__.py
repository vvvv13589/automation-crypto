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


def _load_candles(cfg: dict, args, timeframe: str):
    """Candles from synthetic / CSV / exchange (cached under data/)."""
    from pathlib import Path

    from . import data

    if args.synthetic:
        return data.synthetic(bars=args.bars, timeframe=timeframe, seed=args.seed)
    if getattr(args, "csv", None):
        return data.load_csv(args.csv)
    ex_id, sym = cfg["exchange"]["id"], cfg["symbol"].replace("/", "-").replace(":", "-")
    cache = Path("data") / f"{ex_id}_{sym}_{timeframe}_{args.days}d.csv"
    if cache.exists() and not args.refresh:
        return data.load_csv(str(cache))
    if getattr(args, "source", "api") == "vision":
        print(f"downloading {cfg['symbol']} {timeframe} ({args.days} days) from data.binance.vision ...")
        return data.fetch_binance_vision(cfg["symbol"], timeframe, args.days)
    ex = data.make_exchange(cfg)
    since = ex.milliseconds() - args.days * 86400 * 1000
    print(f"downloading {cfg['symbol']} {timeframe} ({args.days} days) from {ex_id} ...")
    candles = data.fetch_history(ex, cfg["symbol"], timeframe, since)
    cache.parent.mkdir(exist_ok=True)
    candles.reset_index().assign(
        timestamp=lambda d: d["timestamp"].astype("int64") // 10**6
    ).to_csv(cache, index=False)
    return candles


def cmd_optimize(cfg: dict, args) -> int:
    from .optimize import best_yaml, format_report, optimize

    tf_arg = args.timeframes or ("5m,15m" if cfg["daytrade"].get("enabled") else "1h,4h")
    timeframes = [t.strip() for t in tf_arg.split(",") if t.strip()]
    candles = {tf: _load_candles(cfg, args, tf) for tf in timeframes}
    from .optimize import DEFAULT_GRID, GRIDS
    n = 1
    for v in GRIDS.get(cfg["strategy"].get("name"), DEFAULT_GRID).values():
        n *= len(v)
    print(f"testing {n * len(timeframes)} combinations "
          f"(first {int(args.split * 100)}% = in-sample, rest = out-of-sample) ...")
    results = optimize(cfg, candles, ratio=args.split, workers=args.workers)
    print()
    print(format_report(results, top=args.top))
    print("\nIS = in-sample (used for ranking), OOS = out-of-sample (unseen data), "
          "n = trades, B&H = buy & hold")
    print("(circuit breakers are disabled here so every combination runs the full period)")
    from .optimize import MIN_TRADES, robust
    best = next((r for r in results if robust(r)), None)
    if best is None:
        print(f"\n❌ No combination made money on BOTH the in-sample and out-of-sample data "
              f"(with >= {MIN_TRADES} trades).\n   The strategy shows no edge here - do NOT trade it live.")
    else:
        print("\n✅ Best combination that was profitable on both halves. Paste into config.yaml:\n")
        print(best_yaml(best))
    return 0


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
        candles = _load_candles(cfg, args, cfg["timeframe"])
        source = f"{cfg['exchange']['id']} {cfg['symbol']} {cfg['timeframe']} last {args.days}d"

    result = run_backtest(cfg, candles)
    print(f"\nBacktest: {source}  strategy={cfg['strategy'].get('name')}")
    print(json.dumps(result.summary(), indent=2, ensure_ascii=False))
    if args.trades:
        for t in result.trades:
            print(f"{t.opened_at} -> {t.closed_at} {t.side:5} [{t.regime:5}] {t.entry_price:.2f} -> "
                  f"{t.exit_price:.2f} pnl={t.pnl:+.2f} ({t.pnl_pct:+.2f}%) {t.reason}")
    return 0


def cmd_run(cfg: dict, args, live: bool) -> int:
    from .runner import Runner

    if live:
        if not args.confirm_live:
            print("Refusing to trade real money without --confirm-live.", file=sys.stderr)
            return 2
        if cfg.get("market") == "future":
            f = cfg["futures"]
            print(f"LIVE futures: {cfg['symbol']} {f['leverage']}x {f['margin_mode']}, "
                  f"risk {cfg['risk']['risk_per_trade']*100:.1f}%/trade")
        cfg["mode"] = "live"
    Runner(cfg, live=live).run()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cryptobot", description="Adaptive crypto trading bot")
    parser.add_argument("-c", "--config", help="path to config YAML (default: ./config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--symbol", help="override symbol, e.g. ETH/USDT")
    parser.add_argument("--timeframe", help="override timeframe, e.g. 1h")
    parser.add_argument("--daytrade", action="store_true", help="force day-trade mode on")
    parser.add_argument("--no-daytrade", action="store_true", help="force day-trade mode off")
    parser.add_argument("--spot", action="store_true", help="spot long-only instead of futures")
    parser.add_argument("--strategy", choices=["pullback", "adaptive"], help="override strategy.name")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="test the strategy on historical data")
    bt.add_argument("--days", type=int, default=90, help="days of exchange history to download")
    bt.add_argument("--csv", help="use candles from CSV (timestamp,open,high,low,close,volume)")
    bt.add_argument("--refresh", action="store_true", help="re-download instead of using data/ cache")
    bt.add_argument("--source", choices=["api", "vision"], default="api",
                    help="api = exchange API (default), vision = Binance public archive (futures only)")
    bt.add_argument("--synthetic", action="store_true", help="offline synthetic market data")
    bt.add_argument("--bars", type=int, default=5000)
    bt.add_argument("--seed", type=int, default=42)
    bt.add_argument("--trades", action="store_true", help="print every trade")

    op = sub.add_parser("optimize", help="search parameters with an out-of-sample check")
    op.add_argument("--days", type=int, default=None, help="history length (default 365, day-trade 120)")
    op.add_argument("--timeframes", default=None,
                    help="comma separated, e.g. 15m,1h,4h (default 1h,4h; day-trade 5m,15m)")
    op.add_argument("--split", type=float, default=0.7, help="in-sample fraction")
    op.add_argument("--top", type=int, default=10)
    op.add_argument("--workers", type=int, default=None, help="parallel processes (default: all CPUs)")
    op.add_argument("--refresh", action="store_true", help="re-download instead of using data/ cache")
    op.add_argument("--source", choices=["api", "vision"], default="api",
                    help="api = exchange API (default), vision = Binance public archive (futures only)")
    op.add_argument("--csv", help=argparse.SUPPRESS)
    op.add_argument("--synthetic", action="store_true", help="offline synthetic data (for testing)")
    op.add_argument("--bars", type=int, default=4000)
    op.add_argument("--seed", type=int, default=42)

    sub.add_parser("paper", help="real-time simulated trading with live market data")
    sub.add_parser("notify-test", help="send a Telegram test message")
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
    if args.daytrade:
        cfg["daytrade"]["enabled"] = True
    if args.strategy:
        cfg["strategy"]["name"] = args.strategy
    if args.no_daytrade:
        cfg["daytrade"]["enabled"] = False
    if args.spot:
        cfg["market"] = "spot"
        cfg["strategy"]["allow_short"] = False
        if cfg["symbol"].endswith(":USDT"):
            cfg["symbol"] = cfg["symbol"].split(":")[0]
    if getattr(args, "days", 0) is None:
        args.days = 120 if cfg["daytrade"].get("enabled") else 365

    if args.command in ("backtest", "optimize") and not args.verbose:
        logging.getLogger("cryptobot").setLevel(logging.WARNING)  # hide per-trade log lines
    if args.command == "backtest":
        return cmd_backtest(cfg, args)
    if args.command == "optimize":
        return cmd_optimize(cfg, args)
    if args.command == "notify-test":
        from .notify import make_notifier
        n = make_notifier(cfg, "live")
        if n is None:
            print("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            return 1
        ok = n("✅ Telegram 通知設定成功")
        print("sent" if ok else "failed - check token / chat id")
        return 0 if ok else 1
    return cmd_run(cfg, args, live=args.command == "live")


if __name__ == "__main__":
    sys.exit(main())
