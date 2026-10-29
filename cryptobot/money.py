"""Where the account's money went: bot, manual trades, prediction bets, rebates and money moved out.

The futures wallet's own history mixes manual trading with transfers (to the Binance prediction
wallet and back, to the AI trader's sub-account). Transfers are not profit or loss, so they are
taken out first; the prediction bets' result comes from the prediction wallet itself.
Referral commission rebates are paid into the spot wallet, outside the futures history.
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
INTO_SPOT = ("UMFUTURE_MAIN", "FUNDING_MAIN", "CMFUTURE_MAIN", "MARGIN_MAIN")
OUT_OF_SPOT = ("MAIN_UMFUTURE", "MAIN_FUNDING", "MAIN_CMFUTURE", "MAIN_MARGIN")
MAX_REBATE_RISE = 20.0  # an unexplained rise bigger than this is a transfer the history missed, not a rebate
STABLES = ("USDT", "USDC", "FDUSD")


def spot_flows(ex, since_ms: int) -> float:
    """USDT that left the spot wallet since `since_ms` through transfers or withdrawals, minus what
    arrived through transfers or deposits (positive = net out)."""
    net = 0.0
    for kind, sign in [(t, -1) for t in INTO_SPOT] + [(t, 1) for t in OUT_OF_SPOT]:
        rows = ex.sapiGetAssetTransfer({"type": kind, "startTime": since_ms, "size": 100}).get("rows") or []
        net += sign * sum(float(r["amount"]) for r in rows if r.get("asset") == "USDT" and r.get("status") == "CONFIRMED")
    for d in ex.sapiGetCapitalDepositHisrec({"coin": "USDT", "startTime": since_ms}) or []:
        if int(d.get("status", 0)) in (1, 6):  # success / credited
            net -= float(d["amount"])
    for w in ex.sapiGetCapitalWithdrawHistory({"coin": "USDT", "startTime": since_ms}) or []:
        if int(w.get("status", 0)) == 6:  # completed
            net += float(w["amount"]) + float(w.get("transactionFee") or 0)
    return net - prediction_claims(ex, since_ms)  # winning bets are paid into the spot wallet too


def prediction_claims(ex, since_ms: int, get=None) -> float:
    """USDT claimed from winning prediction bets since `since_ms` (it lands in the spot wallet)."""
    get = get or (lambda path, **p: _signed_get(ex, PREDICT + path, **p))
    total = 0.0
    for w in get("/wallet/list").get("wallets") or []:
        offset = 0
        while True:
            page = get("/position/settled-history", walletAddress=w["walletAddress"], limit=100, offset=offset).get("positions") or []
            total += sum(float(p.get("claimAmount") or 0) for p in page if int(p.get("settledDate") or 0) >= since_ms)
            if len(page) < 100:
                break
            offset += 100
    return total


def rebates(ex, cfg: dict, now_ms: int | None = None, spot=None, flows=None) -> float:
    """Total rebates received: every rise of the spot USDT balance that no transfer, deposit or
    withdrawal explains (the spot wallet is not traded). Running total in state/rebates.json; the
    first run counts the spot USDT already there. Rebates arrive in small amounts, so a rise above
    MAX_REBATE_RISE is logged and skipped: only rises are added, one missed transfer would stay forever."""
    now_ms = now_ms or int(time.time() * 1000)
    path = Path(cfg.get("state_dir", "state")) / "rebates.json"
    if spot is None:
        bal = ex.privateGetAccount()["balances"]
        spot = sum(float(b["free"]) + float(b["locked"]) for b in bal if b["asset"] == "USDT")
    st = json.loads(path.read_text()) if path.exists() else None
    if st is None:
        st = {"total": spot}
    else:
        out = spot_flows(ex, st["since"]) if flows is None else flows
        rise = spot - st["spot"] + out
        if rise > MAX_REBATE_RISE:
            log.warning("rebates: spot rose %.2f U with no matching transfer, not counted as a rebate", rise)
        elif rise > 0.001:
            st["total"] += rise
    st.update(spot=spot, since=now_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st))
    return st["total"]


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
        lines.append(f"  🎁 返傭：{rebate:+.2f} U（累計，發到現貨錢包）")
    if moved:
        lines.append(f"  💸 資金{'轉出' if moved < 0 else '轉入'} {abs(moved):.2f} U（預測錢包、AI 子帳戶等，不算盈虧）")
    return lines
