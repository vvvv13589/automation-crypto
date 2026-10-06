"""Real-time loop for paper and live trading."""

from __future__ import annotations

import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from .broker import LiveBroker, PaperBroker
from .data import fetch_recent, make_exchange
from .trader import Trader

log = logging.getLogger(__name__)


class Runner:
    def __init__(self, cfg: dict, live: bool = False):
        self.cfg = cfg
        self.live = live
        self.symbol = cfg["symbol"]
        self.timeframe = cfg["timeframe"]
        if live:
            key, secret = os.getenv("EXCHANGE_API_KEY"), os.getenv("EXCHANGE_API_SECRET")
            if not key or not secret:
                raise SystemExit("live mode needs EXCHANGE_API_KEY / EXCHANGE_API_SECRET (see .env.example)")
            self.exchange = make_exchange(cfg, key, secret, os.getenv("EXCHANGE_API_PASSWORD"))
            broker = LiveBroker(self.exchange)
        else:
            self.exchange = make_exchange(cfg)  # public data only
            p = cfg["paper"]
            broker = PaperBroker(p["starting_cash"], p["fee_rate"], p["slippage"])
        mode = "live" if live else "paper"
        tag = f"{cfg['exchange']['id']}_{self.symbol.replace('/', '-').replace(':', '-')}_{mode}"
        self.trader = Trader(
            cfg,
            broker,
            state_path=str(Path(cfg["state_dir"]) / f"{tag}.json"),
            journal_path=str(Path(cfg["log_dir"]) / f"{tag}_trades.csv"),
        )
        if not live and self.trader.position is not None:
            # Paper balances are in-memory; re-seed holdings for the restored position.
            broker.base = self.trader.position.amount
            broker.cash -= self.trader.position.cost
        self._stop = False
        self._last_candle = None

    def stop(self, *_):
        log.info("stopping after current iteration...")
        self._stop = True

    def step(self) -> None:
        # Real-time protection: check stops/targets on the latest traded price.
        ticker = self.exchange.fetch_ticker(self.symbol)
        price = float(ticker.get("last") or ticker.get("close"))
        now = datetime.now(timezone.utc)
        self.trader.check_exits(price, price, now=now)

        # New closed candle -> re-evaluate market regime and signals.
        candles = fetch_recent(self.exchange, self.symbol, self.timeframe, self.cfg["history_bars"])
        if candles.empty:
            return
        last = candles.index[-1]
        if last != self._last_candle:
            self._last_candle = last
            action = self.trader.on_candle(candles, now=now)
            pos = self.trader.position
            log.info(
                "candle %s close=%.2f equity=%.2f action=%s position=%s",
                last, candles["close"].iloc[-1], self.trader.equity(price), action,
                f"{pos.amount:.6f}@{pos.entry_price:.2f} stop={pos.stop_price:.2f} [{pos.regime}]" if pos else "flat",
            )

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        log.info("starting %s trading %s on %s (%s)", "LIVE" if self.live else "paper",
                 self.symbol, self.cfg["exchange"]["id"], self.timeframe)
        errors = 0
        while not self._stop:
            try:
                self.step()
                errors = 0
            except Exception as exc:  # network hiccups must not kill the bot
                errors += 1
                log.exception("iteration failed (%d in a row): %s", errors, exc)
                if errors >= 20:
                    log.error("too many consecutive errors - exiting")
                    break
                time.sleep(min(60, 2 ** errors))
                continue
            for _ in range(int(self.cfg["poll_seconds"])):
                if self._stop:
                    break
                time.sleep(1)
