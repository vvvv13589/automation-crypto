"""Order execution: a simulated PaperBroker and a ccxt-backed LiveBroker."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class Fill:
    side: str
    amount: float  # base amount actually received (buy) / sold (sell)
    price: float  # average fill price
    fee: float  # fee in quote currency


class PaperBroker:
    """Simulates market orders with fees and slippage. No network needed."""

    def __init__(self, starting_cash: float, fee_rate: float = 0.001, slippage: float = 0.0005):
        self.cash = float(starting_cash)
        self.base = 0.0
        self.fee_rate = fee_rate
        self.slippage = slippage

    def balances(self, symbol: str) -> tuple[float, float]:
        return self.cash, self.base

    def market_buy(self, symbol: str, amount: float, price: float) -> Fill | None:
        fill_price = price * (1 + self.slippage)
        cost = amount * fill_price
        fee = cost * self.fee_rate
        if cost + fee > self.cash:
            amount = self.cash / (fill_price * (1 + self.fee_rate))
            cost, fee = amount * fill_price, amount * fill_price * self.fee_rate
        if amount <= 0:
            return None
        self.cash -= cost + fee
        self.base += amount
        return Fill("buy", amount, fill_price, fee)

    def market_sell(self, symbol: str, amount: float, price: float) -> Fill | None:
        amount = min(amount, self.base)
        if amount <= 0:
            return None
        fill_price = price * (1 - self.slippage)
        proceeds = amount * fill_price
        fee = proceeds * self.fee_rate
        self.base -= amount
        self.cash += proceeds - fee
        return Fill("sell", amount, fill_price, fee)


class LiveBroker:
    """Places real market orders through ccxt. Use with care."""

    def __init__(self, exchange):
        self.ex = exchange

    def balances(self, symbol: str) -> tuple[float, float]:
        base, quote = symbol.split("/")
        quote = quote.split(":")[0]
        bal = self.ex.fetch_balance()
        free = bal.get("free", {})
        return float(free.get(quote) or 0.0), float(free.get(base) or 0.0)

    def _check_min(self, symbol: str, amount: float, price: float) -> bool:
        limits = self.ex.market(symbol).get("limits", {})
        min_amt = (limits.get("amount") or {}).get("min") or 0
        min_cost = (limits.get("cost") or {}).get("min") or 0
        if amount < min_amt or amount * price < min_cost:
            log.warning("order below exchange minimum: amount=%s cost=%s", amount, amount * price)
            return False
        return True

    def _to_fill(self, side: str, order: dict, symbol: str, price: float) -> Fill:
        if order.get("id") and (order.get("average") is None or not order.get("filled")):
            try:
                order = self.ex.fetch_order(order["id"], symbol)
            except Exception as exc:  # some exchanges don't support fetch_order
                log.debug("fetch_order failed: %s", exc)
        filled = float(order.get("filled") or order.get("amount") or 0.0)
        avg = float(order.get("average") or order.get("price") or price)
        fee_info = order.get("fee") or {}
        fee = float(fee_info.get("cost") or 0.0)
        base = symbol.split("/")[0]
        if side == "buy" and fee_info.get("currency") == base:
            filled -= fee  # fee deducted from received base asset
            fee *= avg
        return Fill(side, filled, avg, fee)

    def market_buy(self, symbol: str, amount: float, price: float) -> Fill | None:
        amount = float(self.ex.amount_to_precision(symbol, amount))
        if amount <= 0 or not self._check_min(symbol, amount, price):
            return None
        order = self.ex.create_order(symbol, "market", "buy", amount)
        return self._to_fill("buy", order, symbol, price)

    def market_sell(self, symbol: str, amount: float, price: float) -> Fill | None:
        _, base_free = self.balances(symbol)
        amount = float(self.ex.amount_to_precision(symbol, min(amount, base_free)))
        if amount <= 0 or not self._check_min(symbol, amount, price):
            return None
        order = self.ex.create_order(symbol, "market", "sell", amount)
        return self._to_fill("sell", order, symbol, price)
