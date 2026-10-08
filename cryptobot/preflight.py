"""Read-only checks before trading real money (``python -m cryptobot check``).

Nothing here places, changes or cancels orders. Each check returns
(status, message) where status is "ok", "warn" or "fail".
"""

from __future__ import annotations

import os

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


def _try(fn):
    try:
        return fn(), None
    except Exception as exc:  # report, never crash the check
        return None, str(exc)[:160]


def explain_error(exc) -> str:
    """Plain-language hints for common exchange errors."""
    msg = str(exc)
    if "-2015" in msg or "Invalid API-key" in msg:
        return ("可能原因(依常見程度)：\n"
                "  1. .env 裡的 API Key / Secret 貼錯、多了空格，或 Key 和 Secret 對調\n"
                "  2. API 有設「限制 IP」，但伺服器 IP 不在白名單(用 curl ifconfig.me 查伺服器 IP)\n"
                "  3. 建立 API 時選了「自行生成(Ed25519/RSA)」，請改用「系統生成(HMAC)」\n"
                "  4. API 權限沒勾「允許讀取」或「允許合約」，或帳戶還沒開通合約\n"
                "  5. 剛建立的 key 要等 1～2 分鐘才生效")
    if "-1021" in msg or "Timestamp" in msg:
        return "伺服器時間不準，請執行：sudo timedatectl set-ntp true"
    if "-1022" in msg or "Signature" in msg:
        return "簽章錯誤：Secret Key 貼錯，或建立 API 時選了自行生成(Ed25519/RSA)，請改用系統生成(HMAC)"
    if "451" in msg or "restricted location" in msg.lower():
        return "Binance 拒絕這個地區的連線(伺服器所在地不提供服務)"
    return "請把上面的錯誤訊息貼給 Claude 協助判斷"


def run_checks(exchange, cfg: dict) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    add = lambda status, msg: out.append((status, msg))  # noqa: E731

    # 1. API key works and the futures wallet has money
    bal, err = _try(exchange.fetch_balance)
    if err:
        add(FAIL, f"無法讀取合約帳戶餘額: {err}\n{explain_error(err)}")
        return out
    info = bal.get("info") or {}
    equity = float(info.get("totalMarginBalance") or (bal.get("total") or {}).get("USDT") or 0)
    avail = float(info.get("availableBalance") or (bal.get("free") or {}).get("USDT") or 0)
    if equity <= 0:
        add(FAIL, "U 本位合約帳戶沒有 USDT，請先從現貨帳戶「劃轉」到 U 本位合約")
    else:
        add(OK, f"合約帳戶餘額 {equity:.2f} USDT(可用 {avail:.2f})")
        sc = cfg["scanner"]
        risk = sc["risk_per_trade"] if sc.get("enabled") else cfg["risk"]["risk_per_trade"]
        add(OK, f"每筆最多虧約 {equity * risk:.2f} USDT(總資金的 {risk * 100:.2f}%)")
        if equity < 150:
            add(WARN, "資金少於 150 USDT：很多筆會因低於交易所最低下單金額被跳過")

    # 2. API key permissions
    restr, err = _try(lambda: exchange.sapiGetAccountApiRestrictions())
    if err:
        add(WARN, f"無法讀取 API 權限設定，請自行確認已關閉提現: {err}")
    else:
        if restr.get("enableWithdrawals"):
            add(FAIL, "API key 開啟了「允許提現」！請到 Binance API 管理關閉，被盜用時錢會被領走")
        else:
            add(OK, "API key 沒有提現權限")
        if not restr.get("enableFutures"):
            add(FAIL, "API key 沒有勾選「允許合約」")
        if not restr.get("ipRestrict"):
            add(WARN, "API key 沒有綁定 IP 白名單，建議綁定伺服器 IP")

    # 3. account modes the bot relies on
    multi, err = _try(lambda: exchange.fapiPrivateGetMultiAssetsMargin())
    if err:
        add(WARN, f"無法讀取聯合保證金模式: {err}")
    elif str(multi.get("multiAssetsMargin")).lower() == "true":
        add(FAIL, "合約帳戶是「聯合保證金(多資產)模式」，逐倉無法使用。請到合約設定改成「單幣種保證金」")
    else:
        add(OK, "單幣種保證金模式(逐倉可用)")
    dual, err = _try(lambda: exchange.fapiPrivateGetPositionSideDual())
    if err:
        add(WARN, f"無法讀取持倉模式: {err}")
    elif str(dual.get("dualSidePosition")).lower() == "true":
        add(WARN, "目前是「雙向持倉」，機器人啟動時會改成「單向持倉」(帳戶有持倉或掛單時會失敗)")
    else:
        add(OK, "單向持倉模式")

    # 4. positions or orders the bot did not open
    positions, err = _try(lambda: [p for p in exchange.fetch_positions() if float(p.get("contracts") or 0)])
    if not err and positions:
        names = ", ".join(p["symbol"].split("/")[0] for p in positions[:8])
        add(WARN, f"帳戶已有 {len(positions)} 個持倉({names})：機器人不會管理它們，但它們會占用保證金")

    # 5. market data and the coin universe
    sample = None
    if cfg.get("scanner", {}).get("enabled"):
        from .scanner import pick_universe, summarize_universe

        uni, err = _try(lambda: pick_universe(exchange, cfg))
        if err or not uni:
            add(FAIL, f"無法取得要掃描的幣種名單: {err or '名單是空的'}")
        else:
            add(OK, f"掃描名單 {len(uni)} 種幣: {summarize_universe(uni)}")
            sample = uni[0]
    else:
        sample = cfg["symbol"]
    if sample:
        from .data import fetch_recent

        candles, err = _try(lambda: fetch_recent(exchange, sample, cfg["timeframe"], cfg["history_bars"]))
        if err or candles is None or len(candles) < 100:
            add(FAIL, f"無法取得 {sample} 的 K 線: {err or 'K 線數量不足'}")
        else:
            add(OK, f"K 線資料正常({sample} {cfg['timeframe']}，最新 {candles.index[-1]:%Y-%m-%d %H:%M} UTC)")

    # 6. notifications
    if cfg.get("notify", {}).get("telegram"):
        if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
            add(OK, "Telegram 已設定(可用 notify-test 測試)")
        else:
            add(WARN, "Telegram 沒有設定，開倉/平倉不會通知你的手機")
    return out


def report(results: list[tuple[str, str]]) -> int:
    for status, msg in results:
        print(f"{ICON[status]} {msg}")
    fails = sum(1 for s, _ in results if s == FAIL)
    warns = sum(1 for s, _ in results if s == WARN)
    print()
    if fails:
        print(f"❌ 有 {fails} 個問題必須先修正，修好再執行一次 check。")
        return 1
    print("✅ 檢查通過，可以開始實盤：python -m cryptobot live --confirm-live"
          + (f"(另有 {warns} 個提醒，建議看一下)" if warns else ""))
    return 0
