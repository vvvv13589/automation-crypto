"""Order execution.

All brokers share one interface used by the Trader:

    equity(price)                 account value incl. unrealised PnL (quote currency)
    free_margin(price)            quote available for new positions
    market_open(side, amount, price) / market_close(side, amount, price) -> Fill
    place_limit(side, amount, price) -> order id     (post-only entry)
    poll_limit(order_id, low, high) -> LimitStatus
    cancel_limit(order_id) -> LimitStatus
    set_stop(side, amount, stop_price, old_id) -> id (exchange-side protective stop)
    cancel_stop(order_id)
    position_amount() -> signed position size on the exchange (live only)

``side`` is always the *position* side ("long" / "short").
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

LONG, SHORT = "long", "short"


def _sign(side: str) -> int:
    return 1 if side == LONG else -1


@dataclass
class Fill:
    side: str
    amount: float  # base amount filled
    price: float  # average fill price
    fee: float  # fee in quote currency
    external: bool = False  # position was already closed on the exchange


@dataclass
class LimitStatus:
    filled: float
    price: float
    fee: float
    done: bool  # no longer working on the exchange (filled, cancelled or expired)


class PaperWallet:
    """Cash shared by every PaperBroker of one simulated account (multi-coin scanning)."""

    def __init__(self, cash: float):
        self.cash = float(cash)
        self.members: list["PaperBroker"] = []

    def equity(self) -> float:
        return self.cash + sum(b.unrealized(b.mark) for b in self.members)

    def used_margin(self) -> float:
        return sum(b.pos_amount * b.pos_entry / b.leverage for b in self.members if b.pos_side)


class PaperBroker:
    """Simulated margin account (leverage=1 + long-only behaves like spot).

    One broker trades one symbol. Pass a shared ``wallet`` to let several
    brokers (symbols) draw on the same cash, like one real futures account.
    """

    supports_limit = True
    live = False

    def __init__(self, starting_cash: float, maker_fee: float = 0.0002, taker_fee: float = 0.0005,
                 slippage: float = 0.0003, leverage: float = 1.0, wallet: PaperWallet | None = None):
        self.shared = wallet is not None
        self.account = wallet or PaperWallet(starting_cash)
        self.account.members.append(self)
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.slippage = slippage
        self.leverage = leverage
        self.pos_side: str | None = None
        self.pos_amount = 0.0
        self.pos_entry = 0.0
        self.mark = 0.0  # last price seen for this symbol
        self._orders: dict[str, dict] = {}
        self._ids = itertools.count(1)

    @property
    def wallet(self) -> float:
        return self.account.cash

    @wallet.setter
    def wallet(self, value: float) -> None:
        self.account.cash = value

    # -- persistence (paper accounts survive restarts) --------------------
    def to_dict(self) -> dict:
        data = {"pos_side": self.pos_side, "pos_amount": self.pos_amount,
                "pos_entry": self.pos_entry, "mark": self.mark}
        if not self.shared:  # a shared wallet is saved once, by its owner
            data["wallet"] = self.wallet
        return data

    def load(self, data: dict) -> None:
        if not self.shared and "wallet" in data:
            self.wallet = data["wallet"]
        self.pos_side = data.get("pos_side")
        self.pos_amount = data.get("pos_amount", 0.0)
        self.pos_entry = data.get("pos_entry", 0.0)
        self.mark = data.get("mark", self.pos_entry)

    # -- account ------------------------------------------------------------
    def unrealized(self, price: float) -> float:
        if not self.pos_side:
            return 0.0
        return _sign(self.pos_side) * (price - self.pos_entry) * self.pos_amount

    def equity(self, price: float) -> float:
        self.mark = price
        return self.account.equity()

    def free_margin(self, price: float) -> float:
        self.mark = price
        return max(0.0, self.account.equity() - self.account.used_margin())

    def charge(self, amount: float) -> None:
        """Funding payments and other cash adjustments."""
        self.wallet -= amount

    def _add(self, side: str, amount: float, price: float, fee_rate: float) -> Fill:
        fee = amount * price * fee_rate
        self.wallet -= fee
        if self.pos_side in (None, side):
            total = self.pos_amount + amount
            self.pos_entry = (self.pos_entry * self.pos_amount + price * amount) / total
            self.pos_amount, self.pos_side = total, side
        return Fill(side, amount, price, fee)

    # -- market orders --------------------------------------------------------
    def market_open(self, side: str, amount: float, price: float) -> Fill | None:
        if amount <= 0:
            return None
        fill_price = price * (1 + _sign(side) * self.slippage)
        return self._add(side, amount, fill_price, self.taker_fee)

    def market_close(self, side: str, amount: float, price: float) -> Fill | None:
        amount = min(amount, self.pos_amount)
        if amount <= 0 or self.pos_side != side:
            return Fill(side, 0.0, price, 0.0, external=True)
        fill_price = price * (1 - _sign(side) * self.slippage)
        fee = amount * fill_price * self.taker_fee
        self.wallet += _sign(side) * (fill_price - self.pos_entry) * amount - fee
        self.pos_amount -= amount
        if self.pos_amount <= 1e-12:
            self.pos_side, self.pos_amount, self.pos_entry = None, 0.0, 0.0
        return Fill(side, amount, fill_price, fee)

    # -- limit entries ----------------------------------------------------------
    def place_limit(self, side: str, amount: float, price: float) -> str:
        oid = f"paper-{next(self._ids)}"
        self._orders[oid] = {"side": side, "amount": amount, "price": price}
        return oid

    def poll_limit(self, order_id: str, low: float, high: float) -> LimitStatus:
        order = self._orders.get(order_id)
        if order is None:
            return LimitStatus(0.0, 0.0, 0.0, True)
        touched = low <= order["price"] if order["side"] == LONG else high >= order["price"]
        if not touched:
            return LimitStatus(0.0, order["price"], 0.0, False)
        del self._orders[order_id]
        fill = self._add(order["side"], order["amount"], order["price"], self.maker_fee)
        return LimitStatus(fill.amount, fill.price, fill.fee, True)

    def cancel_limit(self, order_id: str) -> LimitStatus:
        self._orders.pop(order_id, None)
        return LimitStatus(0.0, 0.0, 0.0, True)

    # -- exchange stops (simulated by the Trader itself) -------------------------
    def set_stop(self, side, amount, stop_price, old_id=None):
        return None

    def cancel_stop(self, order_id) -> None:
        return None

    def position_amount(self) -> float:
        return _sign(self.pos_side) * self.pos_amount if self.pos_side else 0.0


class FuturesLiveBroker:
    """Binance (or other ccxt) USDT-margined perpetual futures."""

    supports_limit = True
    live = True

    def __init__(self, exchange, symbol: str, leverage: int, margin_mode: str = "isolated"):
        self.ex = exchange
        self.symbol = symbol
        self.leverage = leverage
        self.margin_mode = margin_mode
        self._bal_cache: tuple[float, dict] | None = None
        self._ready = False

    def _ensure_setup(self) -> None:
        if not self._ready:
            self.setup()
            self._ready = True

    def _meets_minimum(self, amount: float, price: float) -> bool:
        limits = self.ex.market(self.symbol).get("limits") or {}
        min_amount = (limits.get("amount") or {}).get("min") or 0
        min_cost = (limits.get("cost") or {}).get("min") or 0
        if amount < min_amount or amount * price < min_cost:
            log.warning("%s order too small for the exchange (amount %s, value %.2f, min value %s) - skipped",
                        self.symbol, amount, amount * price, min_cost)
            return False
        return True

    def setup(self) -> None:
        """One-way mode, margin mode and leverage. 'Already set' errors are fine."""
        for name, call in (
            ("position mode", lambda: self.ex.set_position_mode(False, self.symbol)),
            ("margin mode", lambda: self.ex.set_margin_mode(self.margin_mode, self.symbol)),
            ("leverage", lambda: self.ex.set_leverage(self.leverage, self.symbol)),
        ):
            try:
                call()
                log.info("%s set", name)
            except Exception as exc:  # e.g. "No need to change margin type"
                log.info("%s unchanged: %s", name, str(exc)[:120])

    # -- account ------------------------------------------------------------
    def _balance(self) -> dict:
        now = time.time()
        if self._bal_cache is None or now - self._bal_cache[0] > 2:
            self._bal_cache = (now, self.ex.fetch_balance())
        return self._bal_cache[1]

    def _quote(self) -> str:
        return self.ex.market(self.symbol)["settle"] or "USDT"

    def equity(self, price: float) -> float:
        bal = self._balance()
        info = bal.get("info") or {}
        if "totalMarginBalance" in info:
            return float(info["totalMarginBalance"])
        return float((bal.get("total") or {}).get(self._quote()) or 0.0)

    def free_margin(self, price: float) -> float:
        bal = self._balance()
        info = bal.get("info") or {}
        if "availableBalance" in info:
            return float(info["availableBalance"])
        return float((bal.get("free") or {}).get(self._quote()) or 0.0)

    def position_amount(self) -> float:
        total = 0.0
        for p in self.ex.fetch_positions([self.symbol]):
            if p.get("symbol") != self.symbol:
                continue
            contracts = float(p.get("contracts") or 0.0) * float(p.get("contractSize") or 1.0)
            total += contracts if p.get("side") == LONG else -contracts
        return total

    # -- helpers -------------------------------------------------------------
    def _amount(self, amount: float) -> float:
        return float(self.ex.amount_to_precision(self.symbol, amount))

    def _order_side(self, side: str, opening: bool) -> str:
        buy = (side == LONG) == opening
        return "buy" if buy else "sell"

    def _refresh(self, order: dict) -> dict:
        try:
            return self.ex.fetch_order(order["id"], self.symbol)
        except Exception as exc:
            log.debug("fetch_order failed: %s", exc)
            return order

    @staticmethod
    def _fee(order: dict, price: float) -> float:
        fee = order.get("fee") or {}
        if fee.get("cost") is not None:
            return float(fee["cost"])
        return sum(float(f.get("cost") or 0) for f in order.get("fees") or [])

    TAKER_FEE_ESTIMATE = 0.0005

    def _order_fee(self, order: dict, filled: float, avg: float) -> float:
        """Binance futures order responses carry no commission: read it from the fills."""
        fee = self._fee(order, avg)
        if fee > 0 or not order.get("id") or filled <= 0:
            return fee
        try:
            trades = self.ex.fetch_order_trades(order["id"], self.symbol)
            total = 0.0
            for t in trades:
                f = t.get("fee") or {}
                cost = float(f.get("cost") or 0)
                if f.get("currency") not in (None, self._quote()):  # e.g. paid in BNB
                    cost = float(t.get("cost") or 0) * self.TAKER_FEE_ESTIMATE
                total += cost
            if total > 0:
                return total
        except Exception as exc:
            log.debug("fetch_order_trades failed: %s", exc)
        return filled * avg * self.TAKER_FEE_ESTIMATE  # best estimate

    def _fill(self, side: str, order: dict, price: float) -> Fill:
        if not order.get("filled") or order.get("average") is None:
            order = self._refresh(order)
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or order.get("price") or price)
        self._bal_cache = None
        return Fill(side, filled, avg, self._order_fee(order, filled, avg))

    # -- market orders ---------------------------------------------------------
    def market_open(self, side: str, amount: float, price: float) -> Fill | None:
        amount = self._amount(amount)
        if amount <= 0 or not self._meets_minimum(amount, price):
            return None
        self._ensure_setup()
        order = self.ex.create_order(self.symbol, "market", self._order_side(side, True), amount)
        return self._fill(side, order, price)

    def market_close(self, side: str, amount: float, price: float) -> Fill | None:
        on_exchange = abs(self.position_amount())
        if on_exchange <= 0:
            return Fill(side, amount, price, 0.0, external=True)
        amount = self._amount(min(amount, on_exchange))
        order = self.ex.create_order(self.symbol, "market", self._order_side(side, False), amount,
                                     None, {"reduceOnly": True})
        return self._fill(side, order, price)

    # -- limit entries ----------------------------------------------------------
    def place_limit(self, side: str, amount: float, price: float) -> str | None:
        amount = self._amount(amount)
        price = float(self.ex.price_to_precision(self.symbol, price))
        if amount <= 0 or not self._meets_minimum(amount, price):
            return None
        self._ensure_setup()
        order = self.ex.create_order(self.symbol, "limit", self._order_side(side, True), amount,
                                     price, {"postOnly": True})
        return order["id"]

    def _status(self, order: dict) -> LimitStatus:
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or order.get("price") or 0.0)
        done = order.get("status") in ("closed", "canceled", "cancelled", "expired", "rejected")
        return LimitStatus(filled, avg, self._fee(order, avg), done)

    def poll_limit(self, order_id: str, low: float, high: float) -> LimitStatus:
        return self._status(self.ex.fetch_order(order_id, self.symbol))

    def cancel_limit(self, order_id: str) -> LimitStatus:
        try:
            self.ex.cancel_order(order_id, self.symbol)
        except Exception as exc:  # already filled / expired
            log.debug("cancel failed: %s", exc)
        status = self._status(self.ex.fetch_order(order_id, self.symbol))
        status.done = True
        self._bal_cache = None
        return status

    # -- exchange-side protective stop -----------------------------------------
    def set_stop(self, side: str, amount: float, stop_price: float, old_id: str | None = None):
        if old_id:
            self.cancel_stop(old_id)
        stop_price = float(self.ex.price_to_precision(self.symbol, stop_price))
        order = self.ex.create_order(
            self.symbol, "market", self._order_side(side, False), self._amount(amount), None,
            {"stopLossPrice": stop_price, "reduceOnly": True},
        )
        return order["id"]

    def cancel_stop(self, order_id: str | None) -> None:
        if not order_id:
            return
        try:
            self.ex.cancel_order(order_id, self.symbol, {"trigger": True})
        except Exception as exc:
            log.debug("cancel stop failed (probably already triggered): %s", exc)


class SpotLiveBroker:
    """Spot market orders (long only, no exchange stop)."""

    supports_limit = False
    live = True

    def __init__(self, exchange, symbol: str):
        self.ex = exchange
        self.symbol = symbol
        self.base, quote = symbol.split("/")
        self.quote = quote.split(":")[0]

    def setup(self) -> None:
        pass

    def _free(self) -> tuple[float, float]:
        free = self.ex.fetch_balance().get("free", {})
        return float(free.get(self.quote) or 0.0), float(free.get(self.base) or 0.0)

    def equity(self, price: float) -> float:
        cash, base = self._free()
        return cash + base * price

    def free_margin(self, price: float) -> float:
        return self._free()[0]

    def position_amount(self) -> float:
        return self._free()[1]

    def _fill(self, side: str, order: dict, price: float) -> Fill:
        if order.get("id") and (order.get("average") is None or not order.get("filled")):
            try:
                order = self.ex.fetch_order(order["id"], self.symbol)
            except Exception as exc:
                log.debug("fetch_order failed: %s", exc)
        filled = float(order.get("filled") or order.get("amount") or 0.0)
        avg = float(order.get("average") or order.get("price") or price)
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or 0.0)
        if fee_info.get("currency") == self.base:
            filled -= fee
            fee *= avg
        return Fill(side, filled, avg, fee)

    def market_open(self, side: str, amount: float, price: float) -> Fill | None:
        if side != LONG:
            raise ValueError("spot market cannot open shorts")
        amount = float(self.ex.amount_to_precision(self.symbol, amount))
        if amount <= 0:
            return None
        return self._fill(side, self.ex.create_order(self.symbol, "market", "buy", amount), price)

    def market_close(self, side: str, amount: float, price: float) -> Fill | None:
        amount = float(self.ex.amount_to_precision(self.symbol, min(amount, self.position_amount())))
        if amount <= 0:
            return Fill(side, 0.0, price, 0.0, external=True)
        return self._fill(side, self.ex.create_order(self.symbol, "market", "sell", amount), price)

    def set_stop(self, side, amount, stop_price, old_id=None):
        return None

    def cancel_stop(self, order_id) -> None:
        return None
