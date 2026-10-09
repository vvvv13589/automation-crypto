"""Market-wide scanning: run the strategy on many symbols from one account.

Every symbol gets its own Trader (position, stops, journal), but they share
one account and one RiskManager, so the circuit breakers see the whole
account. When several symbols signal on the same candle, candidates are
ranked by breakout volume (volume / recent average) and only the best fill
the free slots (``scanner.max_positions``, one position per symbol).

Research (55 Binance USDT perpetuals, 4h, 2024-01..2026-10): breakouts on
higher volume earned more per trade (0.06R -> 0.13-0.16R on average), and
a 5-slot scan was profitable in both halves of the period for 11 of 12
parameter settings.
"""

from __future__ import annotations

import copy
import dataclasses
import glob
import json
import logging
import os
from pathlib import Path

import pandas as pd

from .broker import PaperBroker, PaperWallet
from .risk import RiskManager
from .trader import Trader

log = logging.getLogger(__name__)

# Liquid perpetuals used for scan backtests (live scanning picks the universe by volume).
DEFAULT_UNIVERSE = """BTC ETH SOL BNB XRP DOGE ADA AVAX LINK LTC SUI TRX DOT BCH NEAR APT ARB OP FIL ATOM UNI
AAVE ETC INJ TIA SEI WLD 1000PEPE 1000SHIB WIF TON ORDI RENDER ICP HBAR XLM FET STX IMX ALGO SAND MANA GALA
CRV LDO ENA JUP ONDO TAO 1000BONK PENDLE RUNE THETA EGLD KAS""".split()

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "PYUSD", "EUR", "USD1", "RLUSD", "XUSD"}


def scan_config(cfg: dict) -> dict:
    """Config used in scan mode: per-position risk comes from the scanner section."""
    out = copy.deepcopy(cfg)
    out["risk"]["risk_per_trade"] = cfg["scanner"]["risk_per_trade"]
    return out


def symbol_config(cfg: dict, symbol: str) -> dict:
    out = copy.deepcopy(cfg)
    out["symbol"] = symbol
    return out


def tag_for(cfg: dict, mode: str) -> str:
    return f"{cfg['exchange']['id']}_scan_{mode}"


def file_safe(symbol: str) -> str:
    return symbol.replace("/", "-").replace(":", "-")


class Scanner:
    def __init__(self, cfg: dict, broker_factory, state_dir: str | None = None,
                 log_dir: str | None = None, notify=None, mode: str = "backtest",
                 wallet: PaperWallet | None = None):
        self.cfg = scan_config(cfg)
        self.broker_factory = broker_factory
        self.state_dir, self.log_dir = state_dir, log_dir
        self.notify = notify
        self.mode = mode
        self.wallet = wallet
        self.max_positions = int(cfg["scanner"]["max_positions"])
        self.risk = RiskManager(self.cfg["risk"])
        self.goals = cfg.get("goals") or {}
        self.milestones_sent: set[str] = set()
        self.target_reached = False
        self.defensive = False  # after the target with on_target: defensive
        self.traders: dict[str, Trader] = {}
        self._candidates: list[tuple[float, Trader, object, float]] = []
        self.market_note = ""  # extra context line for entry messages (set by the live runner)
        self._load_portfolio()

    # ------------------------------------------------------------ persistence
    def _portfolio_path(self) -> str | None:
        if not self.state_dir:
            return None
        return str(Path(self.state_dir) / f"{tag_for(self.cfg, self.mode)}_portfolio.json")

    def _load_portfolio(self) -> None:
        path = self._portfolio_path()
        if not path or not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.risk.load(data.get("risk", {}))
        self.milestones_sent = set(data.get("milestones_sent", []))
        self.target_reached = bool(data.get("target_reached", False))
        if data.get("defensive"):
            self._set_defensive()
        if self.wallet is not None and "cash" in data:
            self.wallet.cash = data["cash"]
        # bring back every symbol that still has a position or a working order
        pattern = str(Path(self.state_dir) / f"{tag_for(self.cfg, self.mode)}_*.json")
        for f in glob.glob(pattern):
            if f == path:
                continue
            with open(f, encoding="utf-8") as fh:
                st = json.load(fh)
            if st.get("symbol") and (st.get("position") or st.get("pending")):
                self.trader(st["symbol"])
        log.info("restored scanner: %d open symbols", self.open_count())

    def save(self) -> None:
        path = self._portfolio_path()
        if not path:
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = {"risk": self.risk.to_dict(), "milestones_sent": sorted(self.milestones_sent),
                "target_reached": self.target_reached, "defensive": self.defensive}
        if self.wallet is not None:
            data["cash"] = self.wallet.cash
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)

    # --------------------------------------------------------------- traders
    def trader(self, symbol: str) -> Trader:
        if symbol not in self.traders:
            tag = tag_for(self.cfg, self.mode)
            state = str(Path(self.state_dir) / f"{tag}_{file_safe(symbol)}.json") if self.state_dir else None
            journal = str(Path(self.log_dir) / f"{tag}_trades.csv") if self.log_dir else None
            t = Trader(symbol_config(self.cfg, symbol), self.broker_factory(symbol), state_path=state,
                       journal_path=journal, notify=self.notify, risk=self.risk)
            t.entry_gate = self._gate
            t.defensive = self.defensive
            self.traders[symbol] = t
        return self.traders[symbol]

    def open_symbols(self) -> list[str]:
        return [s for s, t in self.traders.items() if t.position is not None or t.pending is not None]

    def open_count(self) -> int:
        return len(self.open_symbols())

    # ------------------------------------------------------------- candles
    def _gate(self, trader: Trader, decision, cols: dict, i: int) -> bool:
        """Collect entry signals instead of acting on them immediately."""
        vr = cols.get("vol_ratio")
        strength = float(vr[i]) if vr is not None and vr[i] == vr[i] else 0.0
        self._candidates.append((strength, trader, decision, float(cols["close"][i])))
        return False

    def begin_candle(self) -> None:
        self._candidates = []

    def account_equity(self) -> float | None:
        if self.wallet is not None:
            return self.wallet.equity()
        for t in self.traders.values():  # live: any broker reports the whole futures account
            return t.broker.equity(0.0)
        return None

    def _notify(self, text: str) -> None:
        log.info(text.replace("\n", " | "))
        if self.notify:
            self.notify(text)

    def _set_defensive(self) -> None:
        """Lower risk per position and stop pyramiding (after the target)."""
        self.defensive = True
        self.risk.p["risk_per_trade"] = float(self.goals.get("defensive_risk", 0.0075))
        for t in self.traders.values():
            t.defensive = True

    def _lock_in(self, now) -> None:
        """Target reached in defensive mode: close everything, then continue at low risk."""
        self._set_defensive()
        for t in self.traders.values():
            if t.pending is not None:
                t._cancel_pending(now, "target reached")
            if t.position is not None:  # live fills at market; paper uses the last price seen
                t._close(getattr(t.broker, "mark", 0.0) or t.position.entry_price, now, "target reached: lock in")

    def check_goals(self, equity: float | None, now=None) -> None:
        """Telegram milestones: doubled (take the stake out) and target reached (stop entering,
        or with on_target: defensive, close everything and continue at low risk)."""
        base, target = float(self.goals.get("base_capital") or 0), float(self.goals.get("target") or 0)
        if equity is None or base <= 0:
            return
        if self.goals.get("double_alert", True) and equity >= 2 * base and "double" not in self.milestones_sent:
            self.milestones_sent.add("double")
            self._notify(f"🎉 帳戶翻倍了：{equity:.0f} U(本金 {base:.0f} U)\n"
                         f"建議現在提出本金 {base:.0f} U，之後只用獲利繼續跑。\n"
                         f"提領步驟：Ctrl+C 停止機器人 → 在 Binance 把 {base:.0f} U 劃轉出合約帳戶 → "
                         f"執行 python -m cryptobot --live reset-risk → 重新啟動")
        if target > 0 and equity >= target and not self.target_reached:
            self.target_reached = True
            if self.goals.get("on_target", "stop") == "defensive":
                self._lock_in(now)
                risk = float(self.goals.get("defensive_risk", 0.0075)) * 100
                self._notify(f"🏁 達到目標 {target:.0f} U！目前約 {equity:.0f} U\n"
                             f"已全部平倉鎖住獲利，之後改用防守模式(每倉風險 {risk:.2f}%、不加碼)繼續跑。\n"
                             f"建議現在提領本金：sudo systemctl stop cryptobot → 在 Binance 劃轉 → "
                             f"python -m cryptobot --live reset-risk → sudo systemctl start cryptobot")
            else:
                self._notify(f"🏁 達到目標 {target:.0f} U！目前 {equity:.0f} U\n"
                             f"已停止開新倉，現有持倉會照停損自動出場。全部平倉後請提領並停止機器人。")

    def _market_context(self) -> str:
        """Breadth of this scan plus an optional note (e.g. BTC) set by the live runner."""
        ups = sum(1 for _, _, d, _ in self._candidates if d.side == "long")
        downs = len(self._candidates) - ups
        mood = "市場偏強" if ups > downs else ("市場偏弱" if downs > ups else "多空分歧")
        lines = [f"• 這次掃描：{downs} 個幣跌破、{ups} 個突破 → {mood}"] if self._candidates else []
        if self.market_note:
            lines.append(self.market_note)
        return "\n".join(lines)

    def finish_candle(self, now) -> list[str]:
        """Enter the strongest collected signals while slots are free."""
        entered = []
        self.check_goals(self.account_equity(), now)
        if self.target_reached and self.goals.get("stop_at_target", True) and not self.defensive:
            self._candidates = []
            self.save()
            return entered
        free = self.max_positions - self.open_count()
        context = self._market_context()
        for strength, trader, decision, price in sorted(self._candidates, key=lambda c: -c[0]):
            if free <= 0:
                break
            if context:
                decision = dataclasses.replace(decision, reason=f"{decision.reason}\n{context}")
            if trader.try_enter(decision, price, now):
                entered.append(trader.symbol)
                free -= 1
        self._candidates = []
        self.save()
        return entered


# ---------------------------------------------------------------- backtest
def run_scan_backtest(cfg: dict, data: dict[str, pd.DataFrame]) -> dict:
    """Bar-by-bar portfolio backtest over many symbols with one shared account."""
    from .backtest import BacktestResult

    p = cfg["paper"]
    leverage = float(cfg["futures"]["leverage"]) if cfg.get("market") == "future" else 1.0
    wallet = PaperWallet(p["starting_cash"])

    def factory(symbol):
        return PaperBroker(p["starting_cash"], p["maker_fee"], p["taker_fee"], p["slippage"], leverage, wallet=wallet)

    scanner = Scanner(cfg, factory, wallet=wallet)
    prepared = {}
    for sym, df in data.items():
        t = scanner.trader(sym)
        analyzed = t.strategy.analyze(df)
        step = analyzed.index.to_series().diff().median()
        prepared[sym] = dict(
            trader=t, cols=t.strategy.columns(analyzed), step=step,
            warmup=t.strategy.history_warmup(step),
            o=analyzed["open"].to_numpy(), h=analyzed["high"].to_numpy(), l=analyzed["low"].to_numpy(),
            pos={ts: i for i, ts in enumerate(analyzed.index)},
        )
    timeline = sorted(set().union(*(d["pos"].keys() for d in prepared.values())))
    equity, max_open = [], 0
    for ts in timeline:
        live = [(sym, d, d["pos"][ts]) for sym, d in prepared.items() if ts in d["pos"]]
        for sym, d, i in live:
            t = d["trader"]
            t.broker.mark = d["cols"]["close"][i - 1] if i else d["o"][i]
            if i < d["warmup"]:
                continue
            had = t.position is not None
            if t.check_pending(d["l"][i], d["h"][i], now=ts):
                t.check_exits(d["l"][i], d["h"][i], now=ts, intrabar=True, stop_only=True)
            elif had:
                t.check_exits(d["l"][i], d["h"][i], now=ts, open_=d["o"][i], intrabar=True)
        scanner.begin_candle()
        for sym, d, i in live:
            d["trader"].broker.mark = d["cols"]["close"][i]
            if i >= d["warmup"]:
                d["trader"].on_bar(d["cols"], i, now=ts + d["step"])
        scanner.finish_candle(ts + (live[0][1]["step"] if live else pd.Timedelta(0)))
        max_open = max(max_open, scanner.open_count())
        equity.append(wallet.equity())
    last = timeline[-1] if timeline else None
    for sym, d in prepared.items():
        t = d["trader"]
        if t.pending is not None:
            t._cancel_pending(last, "end of backtest")
        if t.position is not None:
            t._close(float(d["cols"]["close"][-1]), last, "end of backtest")
    if equity:
        equity[-1] = wallet.equity()
    trades = [tr for t in scanner.traders.values() for tr in t.trades]
    trades.sort(key=lambda tr: tr.opened_at)
    eq = pd.Series(equity, index=pd.DatetimeIndex(timeline))
    start = max(d["warmup"] for d in prepared.values()) if prepared else 0
    eq = eq.iloc[min(start, len(eq) - 1):]
    result = BacktestResult(eq, trades, p["starting_cash"], 0.0)
    summary = result.summary()
    summary.pop("buy_hold_return_pct", None)
    summary["max_drawdown_halt"] = scanner.risk.drawdown_halt
    summary["floor_halt"] = scanner.risk.floor_halt
    summary["target_reached"] = scanner.target_reached
    summary["symbols"] = len(data)
    summary["max_open_positions"] = max_open
    summary["years"] = {
        str(y): round((eq[eq.index.year == y].iloc[-1] / eq[eq.index.year == y].iloc[0] - 1) * 100, 1)
        for y in sorted(set(eq.index.year)) if (eq.index.year == y).sum() > 1
    }
    return {"summary": summary, "trades": trades, "equity": eq}


# ------------------------------------------------------------------ universe
def allowed_bases(cfg: dict) -> set[str] | None:
    """Coins the scanner may trade: the tested crypto list by default, a custom list, or None (= any)."""
    coins = cfg["scanner"].get("coins", "tested")
    if coins == "tested":
        return set(DEFAULT_UNIVERSE)
    if coins in ("all", None):
        return None
    return {c.upper() for c in coins}


def is_crypto_market(m: dict) -> bool:
    """Binance also lists stock / commodity perpetuals (gold, oil, equities); keep crypto only."""
    info = m.get("info") or {}
    utype = info.get("underlyingType")
    if utype and str(utype).upper() != "COIN":
        return False
    sub = " ".join(map(str, info.get("underlyingSubType") or [])).lower()
    return not any(word in sub for word in ("tradfi", "stock", "equity", "commodity", "metal", "index"))


def pick_universe(exchange, cfg: dict, extra_exclude: set[str] | None = None) -> list[str]:
    """Top USDT perpetuals by 24h quote volume: crypto only, stablecoins and exclusions removed."""
    sc = cfg["scanner"]
    exclude = set(sc.get("exclude", [])) | STABLE_BASES | set(extra_exclude or ())
    allowed = allowed_bases(cfg)
    tickers = exchange.fetch_tickers()
    rows = []
    for sym, tk in tickers.items():
        m = exchange.markets.get(sym)
        if not m or not m.get("swap") or not m.get("linear") or m.get("settle") != "USDT" or not m.get("active", True):
            continue
        base = m.get("base")
        if base in exclude or (allowed is not None and base not in allowed) or not is_crypto_market(m):
            continue
        qv = tk.get("quoteVolume") or 0
        if qv >= sc["min_quote_volume"]:
            rows.append((qv, sym))
    rows.sort(reverse=True)
    return [sym for _, sym in rows[: int(sc["top_n"])]]


def summarize_universe(symbols: list[str]) -> str:
    return ", ".join(s.split("/")[0] for s in symbols[:15]) + (" ..." if len(symbols) > 15 else "")

