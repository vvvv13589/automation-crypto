"""Real-time loop for paper and live trading."""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from .backtest import make_paper_broker
from .broker import FuturesLiveBroker, SpotLiveBroker
from .data import fetch_recent, make_exchange
from .notify import make_notifier
from .trader import Trader

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: dict, live: bool = False):
        self.cfg = cfg
        self.live = live
        self.symbol = cfg["symbol"]
        self.timeframe = cfg["timeframe"]
        futures = cfg.get("market") == "future"
        if live:
            key, secret = os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_API_SECRET")
            if not key or not secret:
                raise SystemExit("live mode needs EXCHANGE_API_KEY / EXCHANGE_API_SECRET (see .env.example)")
            self.exchange = make_exchange(cfg, key, secret, os.getenv("EXCHANGE_API_PASSWORD"))
            if futures:
                f = cfg["futures"]
                broker = FuturesLiveBroker(self.exchange, self.symbol, int(f["leverage"]), f["margin_mode"])
                broker._ensure_setup()
            else:
                broker = SpotLiveBroker(self.exchange, self.symbol)
                broker.setup()
        else:
            self.exchange = make_exchange(cfg)  # public market data only
            broker = make_paper_broker(cfg)
        mode = "live" if live else "paper"
        tag = f"{cfg['exchange']['id']}_{self.symbol.replace('/', '-').replace(':', '-')}_{mode}"
        self.notify = make_notifier(cfg, mode)
        self.trader = Trader(
            cfg,
            broker,
            state_path=str(Path(cfg["state_dir"]) / f"{tag}.json"),
            journal_path=str(Path(cfg["log_dir"]) / f"{tag}_trades.csv"),
            notify=self.notify,
        )
        self._stop = False
        self._last_candle = None

    def stop(self, *_):
        log.info("stopping after current iteration...")
        self._stop = True

    def _say(self, text: str) -> None:
        if self.notify:
            self.notify(text)

    def step(self) -> None:
        ticker = self.exchange.fetch_ticker(self.symbol)
        price = float(ticker.get("last") or ticker.get("close"))
        now = datetime.now(timezone.utc)
        t = self.trader

        # Real-time protection between candles.
        t.reconcile(price, now)  # live: exchange-side stop may have fired
        t.check_pending(price, price, now)
        t.check_exits(price, price, now=now)
        t.session_check(price, now)  # day-trade forced close

        # New closed candle -> re-evaluate market regime and signals.
        candles = fetch_recent(self.exchange, self.symbol, self.timeframe, self.cfg["history_bars"])
        if candles.empty:
            return
        last = candles.index[-1]
        if last != self._last_candle:
            self._last_candle = last
            htf = None
            if t.strategy.htf_timeframe:  # higher-timeframe candles straight from the exchange
                htf = fetch_recent(self.exchange, self.symbol, t.strategy.htf_timeframe, 300)
            action = t.on_candle(candles, now=now, htf=htf)
            pos = t.position
            log.info(
                "candle %s close=%.2f equity=%.2f action=%s position=%s",
                last, candles["close"].iloc[-1], t.equity(price), action,
                f"{pos.side} {pos.amount:.4f}@{pos.entry_price:.2f} stop={pos.stop_price:.2f} [{pos.regime}]"
                if pos else ("pending limit" if t.pending else "flat"),
            )

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        f = self.cfg["futures"]
        desc = (f"{'實盤' if self.live else '模擬'} {self.symbol} {self.timeframe} "
                + (f"{f['leverage']}x {f['margin_mode']}" if self.cfg.get("market") == "future" else "spot"))
        log.info("starting %s", desc)
        self._say(f"▶️ 機器人啟動: {desc}")
        errors = 0
        while not self._stop:
            try:
                self.step()
                errors = 0
            except Exception as exc:  # network hiccups must not kill the bot
                errors += 1
                log.exception("iteration failed (%d in a row): %s", errors, exc)
                if errors in (3, 10):
                    self._say(f"⚠️ 連續 {errors} 次錯誤: {str(exc)[:200]}")
                if errors >= 20:
                    log.error("too many consecutive errors - exiting")
                    self._say("🛑 錯誤太多，機器人已停止。交易所停損單仍有效，請檢查伺服器。")
                    break
                time.sleep(min(60, 2 ** errors))
                continue
            for _ in range(int(self.cfg["poll_seconds"])):
                if self._stop:
                    break
                time.sleep(1)
        self._say("⏹ 機器人已停止" + ("（仍有持倉，交易所停損單有效）" if self.trader.position else ""))


def btc_note(candles) -> str:
    """One line on Bitcoin, the market's bellwether (4h candles)."""
    from .indicators import ema

    c = candles["close"]
    if len(c) < 43:
        return ""
    d1, d7 = c.iloc[-1] / c.iloc[-7] - 1, c.iloc[-1] / c.iloc[-43] - 1
    above = c.iloc[-1] > ema(c, 200).iloc[-1]
    return (f"• BTC：24 小時 {d1 * 100:+.1f}%，7 天 {d7 * 100:+.1f}%，"
            f"在長期均線{'上方' if above else '下方'}")


class ScanRunner:
    """Real-time market-wide scanning (paper or live) with one account."""

    def __init__(self, cfg: dict, live: bool = False):
        from .broker import PaperBroker, PaperWallet
        from .scanner import Scanner

        self.cfg = cfg
        self.live = live
        self.timeframe = cfg["timeframe"]
        self.sc = cfg["scanner"]
        mode = "live" if live else "paper"
        if live:
            key, secret = os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_API_SECRET")
            if not key or not secret:
                raise SystemExit("live mode needs EXCHANGE_API_KEY / EXCHANGE_API_SECRET (see .env.example)")
            if cfg.get("market") != "future":
                raise SystemExit("market scanning trades USDT perpetuals: set market: future")
            self.exchange = make_exchange(cfg, key, secret, os.getenv("EXCHANGE_API_PASSWORD"))
            f = cfg["futures"]
            factory = lambda sym: FuturesLiveBroker(self.exchange, sym, int(f["leverage"]), f["margin_mode"])  # noqa: E731
            wallet = None
        else:
            self.exchange = make_exchange(cfg)
            p = cfg["paper"]
            wallet = PaperWallet(p["starting_cash"])
            lev = float(cfg["futures"]["leverage"])
            factory = lambda sym: PaperBroker(p["starting_cash"], p["maker_fee"], p["taker_fee"],  # noqa: E731
                                              p["slippage"], lev, wallet=wallet)
        self.notify = make_notifier(cfg, mode, label="[掃描]")
        self.scanner = Scanner(cfg, factory, state_dir=cfg["state_dir"], log_dir=cfg["log_dir"],
                               notify=self.notify, mode=mode, wallet=wallet)
        self.universe: list[str] = []
        self._universe_at = 0.0
        self._last_bar = None
        self._stop = False
        self.external: set[str] = set()  # coins with positions the bot did not open
        self.mode = mode
        from .report import DailyReporter
        self.reporter = DailyReporter(cfg)
        if live:
            self._prepare_live_account()

    def _prepare_live_account(self) -> None:
        """One-way position mode is required; never touch positions the bot did not open."""
        ex = self.exchange
        try:
            dual = str(ex.fapiPrivateGetPositionSideDual().get("dualSidePosition")).lower() == "true"
            if dual:
                try:
                    ex.set_position_mode(False)
                except Exception as exc:
                    log.warning("could not switch to one-way mode: %s", exc)
                dual = str(ex.fapiPrivateGetPositionSideDual().get("dualSidePosition")).lower() == "true"
            if dual:
                raise SystemExit("帳戶是「雙向持倉」且無法自動切換(帳戶有持倉或掛單)。請先平倉並取消所有掛單，"
                                 "或到 Binance 合約設定手動改成「單向持倉」，再重新啟動。")
        except SystemExit:
            raise
        except Exception as exc:  # non-Binance exchanges: rely on per-symbol setup
            log.info("position mode check skipped: %s", exc)
        mine = set(self.scanner.open_symbols())
        for p in ex.fetch_positions():
            if float(p.get("contracts") or 0) and p.get("symbol") not in mine:
                self.external.add(p["symbol"].split("/")[0])
        if self.external:
            msg = f"帳戶已有非機器人開的持倉：{', '.join(sorted(self.external))}，機器人不會交易這些幣"
            log.warning(msg)
            self._say(f"⚠️ {msg}")

    def stop(self, *_):
        log.info("stopping after current iteration...")
        self._stop = True

    def _say(self, text: str) -> None:
        if self.notify:
            self.notify(text)

    def refresh_universe(self) -> None:
        from .scanner import pick_universe, summarize_universe

        if self.universe and time.time() - self._universe_at < self.sc["refresh_hours"] * 3600:
            return
        self.universe = pick_universe(self.exchange, self.cfg, extra_exclude=self.external)
        self._universe_at = time.time()
        log.info("universe: %d coins: %s", len(self.universe), summarize_universe(self.universe))

    def protect(self, now) -> None:
        """Between candles: stops, targets, limit fills and exchange-side closes."""
        open_syms = self.scanner.open_symbols()
        if not open_syms:
            return
        tickers = self.exchange.fetch_tickers(open_syms)
        for sym in open_syms:
            tk = tickers.get(sym) or {}
            price = tk.get("last") or tk.get("close")
            if not price:
                continue
            t = self.scanner.trader(sym)
            if not self.live:
                t.broker.mark = float(price)  # keep paper account equity current
            t.reconcile(float(price), now)
            t.check_pending(float(price), float(price), now)
            t.check_exits(float(price), float(price), now=now)
            t.session_check(float(price), now)

    def scan(self, now) -> list[str]:
        """A candle just closed: evaluate every coin, then enter the strongest signals."""
        self.refresh_universe()
        symbols = list(dict.fromkeys(self.scanner.open_symbols() + self.universe))
        self.scanner.begin_candle()
        self.scanner.market_note = ""
        for sym in symbols:
            try:
                candles = fetch_recent(self.exchange, sym, self.timeframe, self.cfg["history_bars"])
                if sym.startswith("BTC/"):
                    self.scanner.market_note = btc_note(candles)
                if len(candles) > 1:
                    self.scanner.trader(sym).on_candle(candles, now=now)
            except Exception as exc:  # one bad market must not stop the scan
                log.warning("%s skipped: %s", sym, str(exc)[:120])
        entered = self.scanner.finish_candle(now)
        open_syms = self.scanner.open_symbols()
        log.info("scan done: %d coins, entered %s, open %d/%d %s", len(symbols), entered or "-",
                 len(open_syms), self.scanner.max_positions, open_syms)
        return entered

    def step(self) -> None:
        now = datetime.now(timezone.utc)
        self.protect(now)
        import pandas as pd

        bar = (pd.Timestamp(now) - pd.Timedelta(seconds=self.sc["settle_seconds"])).floor(self.timeframe.replace("m", "min"))
        if bar != self._last_bar:
            self.scan(now)
            self._last_bar = bar
        if self.reporter.due(now):
            self.send_report(now)

    def send_report(self, now) -> str:
        from .report import build_report

        text = build_report(self.scanner, self.exchange, self.cfg, self.mode, now)
        log.info(text.replace("\n", " | "))
        self._say(text)
        return text

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        f = self.cfg["futures"]
        desc = (f"{'實盤' if self.live else '模擬'} 全市場掃描 前{self.sc['top_n']}大 {self.timeframe} "
                f"{f['leverage']}x {f['margin_mode']} 最多{self.sc['max_positions']}倉 每倉風險{self.sc['risk_per_trade']*100:.2f}%")
        log.info("starting %s", desc)
        self._say(f"▶️ 機器人啟動: {desc}")
        errors = 0
        while not self._stop:
            try:
                self.step()
                errors = 0
            except Exception as exc:
                errors += 1
                log.exception("iteration failed (%d in a row): %s", errors, exc)
                if errors in (3, 10):
                    self._say(f"⚠️ 連續 {errors} 次錯誤: {str(exc)[:200]}")
                if errors >= 20:
                    self._say("🛑 錯誤太多，機器人已停止。交易所停損單仍有效，請檢查伺服器。")
                    break
                time.sleep(min(60, 2 ** errors))
                continue
            for _ in range(int(self.cfg["poll_seconds"])):
                if self._stop:
                    break
                time.sleep(1)
        n = self.scanner.open_count()
        self._say("⏹ 機器人已停止" + (f"（仍有 {n} 個持倉，交易所停損單有效）" if n else ""))
