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
