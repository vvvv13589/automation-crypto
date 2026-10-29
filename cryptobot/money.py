"""Where the account's money went: bot, manual trades, prediction bets, rebates and money moved out.

The futures wallet's own history mixes manual trading with transfers (to the Binance prediction
wallet and back, to the AI trader's sub-account). Transfers are not profit or loss, so they are
taken out first; the prediction bets' result comes from the prediction wallet itself.
Referral rebates are paid through Binance Pay, outside the futures history.
Read-only: only signed GET requests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

PREDICT = "https://api.binance.com/sapi/v1/w3w/wallet/prediction"
REBATE_PAY_TYPES = ("SEND_MULTIPLE_V2",)  # referral rebates arrive as Binance Pay batch payouts (red packets are CRYPTO_BOX)
STABLES = ("USDT", "USDC", "FDUSD")


def rebates(ex, cfg: dict, rows: list[dict] | None = None) -> float:
    """Total referral rebates received: incoming Binance Pay batch payouts in stablecoins. Each payout
    is remembered in state/rebates.json by its transaction id, so the total outlives the Pay history."""
    path = Path(cfg.get("state_dir", "state")) / "rebates.json"
    st = json.loads(path.read_text()) if path.exists() else {}
    payouts = st.get("payouts") or {}
    if rows is None:
        rows = ex.sapiGetPayTransactions({"limit": 100}).get("data") or []
    for r in rows:
        amount = float(r.get("amount") or 0)
        if r.get("orderType") in REBATE_PAY_TYPES and r.get("currency") in STABLES and amount > 0:
            payouts[str(r.get("transactionId") or r.get("orderId"))] = amount
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"payouts": payouts}))
    return sum(payouts.values())


def holdings(ex, futures: float, ai: float | None, predict: dict | None) -> list[tuple[str, float | None]]:
    """Every place the money sits, in U: (label, amount; None when it could not be read).
    Wallets count stablecoins only; prediction bets count at their current price."""
    def stables(rows, asset="asset", *amounts):
        return sum(float(r.get(a) or 0) for r in rows if r.get(asset) in STABLES for a in amounts)

    out: list[tuple[str, float | None]] = [("主帳戶·合約", futures)]
    for label, read in [
        ("主帳戶·資金錢包", lambda: stables(ex.sapiPostAssetGetFundingAsset({}), "asset", "free", "locked", "freeze")),
        ("主帳戶·現貨", lambda: stables(ex.privateGetAccount()["balances"], "asset", "free", "locked")),
        ("主帳戶·理財", lambda: stables(ex.sapiGetSimpleEarnFlexiblePosition({"size": 100}).get("rows") or [], "asset", "totalAmount")
         + stables(ex.sapiGetSimpleEarnLockedPosition({"size": 100}).get("rows") or [], "asset", "amount")),
    ]:
        try:
            out.append((label, read()))
        except Exception as exc:
            log.warning("holdings %s: %s", label, exc)
            out.append((label, None))
    out.append(("AI 子帳戶", ai))
    out.append(("預測下注（持倉市值）", None if predict is None else predict["open_cost"] + predict["unrealized"]))
    return out


def total_changes(cfg: dict, day: str, total: float, days: int = 14) -> tuple[float | None, float | None]:
    """(change since the day's first reading, change over 7 days). The first reading of each day is
    kept in state/money_total.json; deposits and withdrawals show up as changes too."""
    from datetime import date, timedelta
    path = Path(cfg.get("state_dir", "state")) / "money_total.json"
    seen = json.loads(path.read_text()) if path.exists() else {}
    seen.setdefault(day, total)
    oldest = (date.fromisoformat(day) - timedelta(days=days)).isoformat()
    seen = {d: v for d, v in seen.items() if d >= oldest}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(seen))
    week_ago = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    past = [d for d in seen if d <= week_ago]
    return total - seen[day], (total - seen[max(past)]) if past else None


def holdings_text(rows: list[tuple[str, float | None]], today: float | None, week: float | None) -> str:
    total = sum(v for _, v in rows if v)
    lines = [f"💰 全部資產 {total:.2f} U"]
    if today is not None:
        lines[0] += f"（今天 {today:+.2f}" + (f"、7 天 {week:+.2f}" if week is not None else "") + "）"
    for label, v in rows:
        if v is None:
            lines.append(f"  • {label}：讀取失敗，未計入")
        elif abs(v) >= 0.01:
            lines.append(f"  • {label}：{v:.2f} U")
    lines.append("  （只算穩定幣；轉入轉出幣安也會算進漲跌）")
    return "\n".join(lines)


def transfers_after_base(income: list[dict], base: float) -> float:
    """Net transfers into the futures wallet after the opening deposits that make up `base`."""
    total, funded = 0.0, False
    for r in sorted((r for r in income if r.get("incomeType") == "TRANSFER"), key=lambda r: int(r["time"])):
        amt = float(r.get("income") or 0)
        if not funded:
            total += amt
            funded = total >= base - 0.01
            if funded:
                total -= base  # anything above the stake in the opening deposits counts as later money
            continue
        total += amt
    return total if funded else 0.0


def _signed_get(ex, url: str, **params) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    q = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    q += "&signature=" + hmac.new(ex.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"{url}?{q}", headers={"X-MBX-APIKEY": ex.apiKey})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def prediction_pnl(ex, get=None) -> dict | None:
    """Result of all prediction bets: settled (claimed or lost) and open (marked at the current price)."""
    get = get or (lambda path, **p: _signed_get(ex, PREDICT + path, **p))
    try:
        wallets = get("/wallet/list").get("wallets") or []
    except Exception as exc:
        log.warning("prediction wallet: %s", exc)
        return None
    out = {"realized": 0.0, "unrealized": 0.0, "settled": 0, "wins": 0, "open": 0, "open_cost": 0.0}
    for w in wallets:
        addr = w["walletAddress"]
        offset = 0
        while True:
            page = get("/position/settled-history", walletAddress=addr, limit=100, offset=offset).get("positions") or []
            for p in page:
                cost = float(p.get("totalCost") or 0)
                out["realized"] += float(p.get("claimAmount") or 0) - cost
                out["settled"] += 1
                out["wins"] += bool(p.get("isWinner"))
            if len(page) < 100:
                break
            offset += 100
        for p in get("/position/list", walletAddress=addr, limit=100).get("positions") or []:
            cost = float(p.get("totalCost") or 0)
            out["unrealized"] += float(p.get("shares") or 0) * float(p.get("currentPrice") or 0) - cost
            out["open"] += 1
            out["open_cost"] += cost
    return out


def money_lines(ex, cfg: dict, equity: float, bot_total: float, income=None, predict=None,
                rebate=None) -> list[str]:
    """Lines under the account total: bot / manual / prediction / rebates / moved out."""
    base = float((cfg.get("goals") or {}).get("base_capital") or 0)
    if base <= 0:
        return []
    lines = []
    try:
        if income is None:
            from .pnl import fetch_income
            now = int(time.time() * 1000)
            income = fetch_income(ex, now - 89 * 86400_000, now)
        moved = transfers_after_base(income, base)
    except Exception as exc:
        log.warning("income: %s", exc)
        moved = None
    if moved is None:
        lines.append(f"  ✋ 手動／其他：{equity - base - bot_total:+.2f} U（含轉入轉出，轉帳紀錄讀取失敗）")
    else:
        lines.append(f"  ✋ 手動交易：{equity - base - moved - bot_total:+.2f} U")
    p = prediction_pnl(ex) if predict is None else predict
    if p:
        lines.append(f"  🎲 預測：{p['realized'] + p['unrealized']:+.2f} U（已結算 {p['settled']} 筆、"
                     f"贏 {p['wins']} 筆 {p['realized']:+.2f}；未結算 {p['open']} 筆 {p['unrealized']:+.2f}）")
    if rebate is None:
        try:
            rebate = rebates(ex, cfg)
        except Exception as exc:
            log.warning("rebates: %s", exc)
    if rebate:
        lines.append(f"  🎁 返傭：{rebate:+.2f} U（累計，幣安 Pay 批量入帳，不含紅包）")
    if moved:
        lines.append(f"  💸 資金{'轉出' if moved < 0 else '轉入'} {abs(moved):.2f} U（預測錢包、AI 子帳戶等，不算盈虧）")
    return lines
