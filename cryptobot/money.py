"""Where the account's money went: bot, manual trades, prediction bets and money moved out.

The futures wallet's own history mixes manual trading with transfers (to the Binance prediction
wallet and back, to the AI trader's sub-account). Transfers are not profit or loss, so they are
taken out first; the prediction bets' result comes from the prediction wallet itself.
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

log = logging.getLogger(__name__)

PREDICT = "https://api.binance.com/sapi/v1/w3w/wallet/prediction"


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


def money_lines(ex, cfg: dict, equity: float, bot_total: float, income=None, predict=None) -> list[str]:
    """Lines under the account total: bot / manual / prediction / moved out."""
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
    if moved:
        lines.append(f"  💸 資金{'轉出' if moved < 0 else '轉入'} {abs(moved):.2f} U（預測錢包、AI 子帳戶等，不算盈虧）")
    return lines
