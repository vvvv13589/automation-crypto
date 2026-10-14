"""Position management from Telegram (butler phase 2): stop, take profit, close.

Every operation is planned first (``plan_*``: validated against the live position, with a
summary and estimated PnL for the confirmation buttons) and only executed after the user
confirms. Execution re-reads the position, sends reduce-only orders, and reports what the
exchange actually holds afterwards. Only the position the user named is touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .butler import _order, _px

BOT_WARNING = ("🤖 注意：這是交易機器人開的倉，機器人也在管理它：它記得自己的停損價，價格碰到時"
               "仍會市價平倉；移動停損時會再掛回它自己的停損單；部分平倉它不會察覺（它的單都是只減倉，不會反向開倉）。")


class OpError(ValueError):
    """A request that cannot be done as asked (shown to the user as is)."""


@dataclass
class Plan:
    kind: str  # stop | tp | close
    symbol: str
    coin: str
    side: str  # position side: long | short
    owner: str  # bot | manual
    price: float | None  # stop / take-profit trigger; None for a market close
    fraction: float  # of the position (stop: always 1)
    amount: float  # contracts at planning time (re-computed at execution)
    summary: str
    extra: dict = field(default_factory=dict)


def parse_coin(text: str) -> str:
    c = text.upper().split("/")[0].split(":")[0]
    c = c[:-4] if c.endswith("USDT") and len(c) > 4 else c
    if not re.fullmatch(r"[A-Z0-9]{1,20}", c):
        raise OpError(f"看不懂幣種：{text}")
    return c


def parse_price(text: str) -> float:
    try:
        v = float(text.replace(",", ""))
    except ValueError:
        raise OpError(f"看不懂價格：{text}") from None
    if v <= 0:
        raise OpError("價格要大於 0")
    return v


def parse_fraction(text: str, amount: float) -> float:
    """"50%" or a contract amount -> fraction of the position (0 < f <= 1)."""
    t = text.strip().replace(",", "")
    try:
        f = float(t[:-1]) / 100 if t.endswith("%") else float(t) / amount
    except (ValueError, ZeroDivisionError):
        raise OpError(f"看不懂數量：{text}") from None
    if f <= 0:
        raise OpError("數量要大於 0")
    if f > 1 + 1e-9:
        raise OpError(f"數量超過持倉（持倉 {_px(amount)}）")
    return min(f, 1.0)


def find_position(snapshot: dict, coin: str) -> dict:
    for p in snapshot["positions"]:
        if p["coin"] == coin:
            return p
    held = ", ".join(p["coin"] for p in snapshot["positions"]) or "無"
    raise OpError(f"{coin} 沒有持倉（目前持倉：{held}）")


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def _head(p: dict) -> str:
    side = "多單" if p["side"] == "long" else "空單"
    owner = "🤖 機器人" if p["owner"] == "bot" else "✋ 手動"
    return (f"{p['coin']} {side}（{owner}）持倉 {_px(p['amount'])}，均價 {_px(p['entry'])}，"
            f"現價 {_px(p['mark'])}，未實現 {p['unrealized']:+.2f} U")


def _stops(p: dict) -> list[dict]:
    return [o for o in p["orders"] if o["kind"] == "stop"]


def plan_stop(snapshot: dict, coin: str, price: float) -> Plan:
    p = find_position(snapshot, coin)
    s = _sign(p["side"])
    if (p["mark"] - price) * s <= 0:
        where = "低於" if s > 0 else "高於"
        raise OpError(f"{coin} {'多' if s > 0 else '空'}單的停損要{where}現價 {_px(p['mark'])}，否則會立刻觸發")
    pnl = (price - p["entry"]) * p["amount"] * s
    old = _stops(p)
    lines = [f"🛑 {'移動' if old else '掛上'}停損", _head(p),
             f"新停損 {_px(price)}：整個部位 {_px(p['amount'])}，只減倉，觸發後市價平倉",
             f"觸發時預估損益 {pnl:+.2f} U（未含手續費與滑價）",
             ("先掛新停損，成功後再取消舊的：" + "、".join(_px(o["price"]) for o in old)) if old else "目前沒有停損單"]
    liq = p.get("liquidation") or 0
    if liq and (price - liq) * s <= 0:
        lines.append(f"⚠️ 停損在強平價 {_px(liq)} 之外，會先被強平")
    if p["owner"] == "bot":
        lines.append(BOT_WARNING)
    return Plan("stop", p["symbol"], coin, p["side"], p["owner"], price, 1.0, p["amount"], "\n".join(lines))


def plan_tp(snapshot: dict, coin: str, price: float, qty: str) -> Plan:
    p = find_position(snapshot, coin)
    s = _sign(p["side"])
    if (price - p["mark"]) * s <= 0:
        where = "高於" if s > 0 else "低於"
        raise OpError(f"{coin} {'多' if s > 0 else '空'}單的止盈要{where}現價 {_px(p['mark'])}")
    f = parse_fraction(qty, p["amount"])
    amount = p["amount"] * f
    pnl = (price - p["entry"]) * amount * s
    lines = [f"🎯 掛止盈", _head(p),
             f"止盈 {_px(price)}：數量 {_px(amount)}（{f * 100:.0f}%），只減倉，觸發後市價平倉",
             f"觸發時預估損益 {pnl:+.2f} U（未含手續費與滑價）"]
    if p["owner"] == "bot":
        lines.append(BOT_WARNING)
    return Plan("tp", p["symbol"], coin, p["side"], p["owner"], price, f, amount, "\n".join(lines))


def plan_close(snapshot: dict, coin: str, qty: str = "100%") -> Plan:
    p = find_position(snapshot, coin)
    f = parse_fraction(qty, p["amount"])
    amount = p["amount"] * f
    pnl = p["unrealized"] * f
    full = f >= 1 - 1e-9
    lines = [f"💥 {'全部' if full else '部分'}平倉（市價、只減倉）", _head(p),
             f"平倉數量 {_px(amount)}（{f * 100:.0f}%），以現價估算損益 {pnl:+.2f} U（未含手續費與滑價）"]
    if full and p["orders"]:
        lines.append(f"平倉後會取消 {p['coin']} 剩下的 {len(p['orders'])} 張停損／止盈／掛單")
    if p["owner"] == "bot":
        lines.append(BOT_WARNING if not full else "🤖 這是交易機器人開的倉：全部平倉後機器人會自動記錄這筆交易並停止管理它")
    return Plan("close", p["symbol"], coin, p["side"], p["owner"], None, f, amount, "\n".join(lines))


# ------------------------------------------------------------------ execution
def _live_position(exchange, symbol: str) -> dict | None:
    for p in exchange.fetch_positions([symbol]):
        if p.get("symbol") == symbol and float(p.get("contracts") or 0):
            return p
    return None


def _orders(exchange, symbol: str) -> list[dict]:
    out = []
    for params in ({}, {"trigger": True}):
        out += [{**_order(o), "trigger": bool(params)} for o in exchange.fetch_open_orders(symbol, params=params)]
    return out


def _cancel(exchange, symbol: str, o: dict) -> None:
    exchange.cancel_order(o["id"], symbol, {"trigger": True} if o.get("trigger") else {})


def _after(exchange, plan: Plan) -> str:
    """What the exchange holds now, read back after the operation."""
    pos = _live_position(exchange, plan.symbol)
    if pos is None:
        return f"📋 交易所回報：{plan.coin} 已無持倉"
    orders = _orders(exchange, plan.symbol)

    def line(kind):
        picked = sorted((o for o in orders if o["kind"] == kind), key=lambda o: o["price"])
        return "、".join(f"{_px(o['price'])}（{'全部' if o['amount'] is None else _px(o['amount'])}）"
                        for o in picked) or "—"
    return (f"📋 交易所回報：{plan.coin} 持倉 {_px(float(pos['contracts']))}，"
            f"未實現 {float(pos.get('unrealizedPnl') or 0):+.2f} U\n停損 {line('stop')}｜止盈 {line('tp')}")


def execute(exchange, plan: Plan) -> str:
    """Run a confirmed plan against the current position. Raises OpError if it changed."""
    exchange.load_markets()
    pos = _live_position(exchange, plan.symbol)
    if pos is None or pos.get("side") != plan.side:
        raise OpError(f"{plan.coin} 的持倉已經變了（已平倉或方向不同），沒有下單")
    held = float(pos["contracts"])
    close_side = "sell" if plan.side == "long" else "buy"
    amount = float(exchange.amount_to_precision(plan.symbol, held * plan.fraction))
    if amount <= 0:
        raise OpError(f"數量太小（{_px(held * plan.fraction)}），交易所無法下單")

    if plan.kind == "stop":
        old = [o for o in _orders(exchange, plan.symbol) if o["kind"] == "stop"]
        price = float(exchange.price_to_precision(plan.symbol, plan.price))
        order = exchange.create_order(plan.symbol, "market", close_side, amount, None,
                                      {"stopLossPrice": price, "reduceOnly": True})
        failed = []
        for o in old:  # only after the new stop is accepted: never a moment without one
            try:
                _cancel(exchange, plan.symbol, o)
            except Exception as exc:
                failed.append(f"{_px(o['price'])}（{str(exc)[:80]}）")
        head = f"✅ 新停損已掛上 {_px(price)}，數量 {_px(amount)}（單號 {order.get('id')}）"
        if old:
            head += f"\n已取消舊停損 {len(old) - len(failed)} 張"
        if failed:
            head += f"\n⚠️ 舊停損取消失敗：{'、'.join(failed)}（只減倉，不會反向開倉）"
        return f"{head}\n{_after(exchange, plan)}"

    if plan.kind == "tp":
        price = float(exchange.price_to_precision(plan.symbol, plan.price))
        order = exchange.create_order(plan.symbol, "market", close_side, amount, None,
                                      {"takeProfitPrice": price, "reduceOnly": True})
        return f"✅ 止盈已掛上 {_px(price)}，數量 {_px(amount)}（單號 {order.get('id')}）\n{_after(exchange, plan)}"

    # market close
    order = exchange.create_order(plan.symbol, "market", close_side, amount, None, {"reduceOnly": True})
    try:
        order = exchange.fetch_order(order["id"], plan.symbol)
    except Exception:
        pass
    filled = float(order.get("filled") or amount)
    avg = float(order.get("average") or order.get("price") or 0)
    entry = float(pos.get("entryPrice") or 0)
    pnl = (avg - entry) * filled * _sign(plan.side) if avg and entry else None
    head = f"✅ 已市價平倉 {_px(filled)} {plan.coin}" + (f" @ {_px(avg)}" if avg else "")
    if pnl is not None:
        head += f"，約 {pnl:+.2f} U（未含手續費）"
    if _live_position(exchange, plan.symbol) is None:
        left = _orders(exchange, plan.symbol)
        failed = 0
        for o in left:  # no position left: remove its orphaned stops / targets / orders
            try:
                _cancel(exchange, plan.symbol, o)
            except Exception:
                failed += 1
        if left:
            head += f"\n已取消剩下的 {len(left) - failed} 張掛單" + (f"，{failed} 張取消失敗" if failed else "")
    return f"{head}\n{_after(exchange, plan)}"
