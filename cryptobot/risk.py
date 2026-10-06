"""Position sizing and account-level circuit breakers."""

from __future__ import annotations

from datetime import datetime


class RiskManager:
    def __init__(self, params: dict):
        self.p = params
        self.peak_equity: float | None = None
        self.day: str | None = None
        self.day_start_equity: float | None = None
        self.drawdown_halt = False
        self.daily_halt = False

    # -- state persistence -------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "drawdown_halt": self.drawdown_halt,
        }

    def load(self, data: dict) -> None:
        self.peak_equity = data.get("peak_equity")
        self.day = data.get("day")
        self.day_start_equity = data.get("day_start_equity")
        self.drawdown_halt = bool(data.get("drawdown_halt", False))

    # -- updates -----------------------------------------------------------
    def update(self, equity: float, now: datetime) -> None:
        day = now.strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start_equity, self.daily_halt = day, equity, False
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity
        if self.peak_equity and equity <= self.peak_equity * (1 - self.p["max_drawdown"]):
            self.drawdown_halt = True
        if self.day_start_equity and equity <= self.day_start_equity * (1 - self.p["daily_loss_limit"]):
            self.daily_halt = True

    def can_open(self) -> tuple[bool, str]:
        if self.drawdown_halt:
            return False, "max drawdown reached - trading halted (reset state to resume)"
        if self.daily_halt:
            return False, "daily loss limit reached - paused until tomorrow"
        return True, ""

    def position_size(self, equity: float, free_margin: float, price: float, stop_price: float,
                      leverage: float = 1.0) -> float:
        """Base amount so that a stop-out loses ~risk_per_trade of equity.

        Exposure is capped at ``max_exposure`` x equity and by the free margin.
        """
        dist = abs(price - stop_price) if stop_price is not None else 0.0
        if price <= 0 or dist <= 0:
            return 0.0
        amount = equity * self.p["risk_per_trade"] / dist
        cap_notional = min(equity * min(self.p["max_exposure"], leverage),
                           free_margin * leverage * 0.95)
        amount = min(amount, cap_notional / price)
        if amount * price < self.p["min_notional"]:
            return 0.0
        return amount
