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
    first run counts the spot USDT already there."""
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
        if rise > 0.001:
            st["total"] += rise
    st.update(spot=spot, since=now_ms)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st))
    return st["total"]


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
