"""Money split: transfers are not profit; prediction bets come from the prediction wallet."""

from cryptobot.money import money_lines, prediction_pnl, transfers_after_base


def tr(t, amt):
    return {"incomeType": "TRANSFER", "time": t, "income": str(amt)}


INCOME = [tr(1, 15), tr(2, 300.67), {"incomeType": "REALIZED_PNL", "time": 3, "income": "40"},
          tr(4, -20), tr(5, 10.65), tr(6, -50)]


def test_opening_deposits_are_the_stake():
    assert transfers_after_base(INCOME, 315.67) == -59.35  # -20 + 10.65 - 50
    assert transfers_after_base(INCOME[:1], 315.67) == 0.0  # stake not funded yet


def test_prediction_pnl_settled_and_open():
    pages = {
        "/wallet/list": {"wallets": [{"walletAddress": "0xabc"}]},
        "/position/settled-history": {"positions": [
            {"totalCost": "5", "claimAmount": "18.84", "isWinner": True},
            {"totalCost": "10", "claimAmount": None, "isWinner": False}]},
        "/position/list": {"positions": [{"totalCost": "10", "shares": "14.36", "currentPrice": "0.99"}]},
    }
    p = prediction_pnl(None, get=lambda path, **kw: pages[path])
    assert round(p["realized"], 2) == 3.84 and p["settled"] == 2 and p["wins"] == 1
    assert round(p["unrealized"], 2) == 4.22 and p["open"] == 1


def test_lines_add_up_to_the_account():
    cfg = {"goals": {"base_capital": 315.67}}
    pred = {"realized": 1.08, "unrealized": 4.2, "settled": 6, "wins": 3, "open": 2, "open_cost": 20}
    lines = money_lines(None, cfg, equity=260.0, bot_total=-89.77, income=INCOME, predict=pred)
    # 260 = 315.67 stake - 59.35 moved out - 89.77 bot + manual  ->  manual = +93.45
    assert lines[0] == "  ✋ 手動交易：+93.45 U"
    assert "🎲 預測：+5.28 U" in lines[1]
    assert "資金轉出 59.35 U" in lines[2]


def test_rebates_are_pay_batch_payouts_not_red_packets(tmp_path):
    from cryptobot.money import rebates
    cfg = {"state_dir": str(tmp_path)}
    rows = [{"transactionId": "1", "orderType": "SEND_MULTIPLE_V2", "currency": "USDT", "amount": "6.14"},
            {"transactionId": "2", "orderType": "CRYPTO_BOX", "currency": "USDT", "amount": "0.002"},
            {"transactionId": "3", "orderType": "CRYPTO_BOX", "currency": "BTTC", "amount": "59.9"}]
    assert rebates(None, cfg, rows=rows) == 6.14
    assert rebates(None, cfg, rows=rows) == 6.14  # the same payout is not counted twice
    later = [{"transactionId": "4", "orderType": "SEND_MULTIPLE_V2", "currency": "USDT", "amount": "3"}]
    assert rebates(None, cfg, rows=later) == 9.14  # older payouts stay after they leave the Pay history


class FakeWallets:
    def sapiPostAssetGetFundingAsset(self, _p):
        return [{"asset": "USDT", "free": "200", "locked": "0", "freeze": "0"}, {"asset": "BTTC", "free": "59.9"}]

    def privateGetAccount(self):
        return {"balances": [{"asset": "USDT", "free": "1.5", "locked": "0"}]}

    def sapiGetSimpleEarnFlexiblePosition(self, _p):
        raise RuntimeError("no permission")

    def sapiGetSimpleEarnLockedPosition(self, _p):
        return {"rows": []}


def test_holdings_add_every_wallet_and_flag_unreadable_ones():
    from cryptobot.money import holdings, holdings_text
    rows = holdings(FakeWallets(), 157.44, 96.87, {"open_cost": 10.0, "unrealized": 1.72})
    assert dict(rows) == {"主帳戶·合約": 157.44, "主帳戶·資金錢包": 200.0, "主帳戶·現貨": 1.5, "主帳戶·理財": None,
                          "AI 子帳戶": 96.87, "預測下注（持倉市值）": 11.72}
    text = holdings_text(rows, today=3.0, week=None)
    assert text.startswith("💰 全部資產 467.53 U（今天 +3.00）")
    assert "主帳戶·理財：讀取失敗，未計入" in text


def test_total_changes_keep_the_first_reading_of_each_day(tmp_path):
    from cryptobot.money import total_changes
    cfg = {"state_dir": str(tmp_path)}
    assert total_changes(cfg, "2026-10-22", 400.0) == (0.0, None)
    assert total_changes(cfg, "2026-10-29", 460.0) == (0.0, 60.0)
    assert total_changes(cfg, "2026-10-29", 466.0) == (6.0, 66.0)


def test_external_flows_count_money_moved_since_the_start(tmp_path):
    from cryptobot.money import external_flows, flows_text
    cfg = {"state_dir": str(tmp_path)}
    found = {"提領": [], "入金": [], "Pay": [], "P2P": []}
    first = external_flows(None, cfg, 474.0, now_ms=1_000, read=lambda ex, since: found)
    assert (first["start_total"], first["net_out"], first["pnl"]) == (474.0, 0.0, 0.0)
    found = {"提領": [("w1", -200.5)], "入金": [], "Pay": [("p9", 10.0)], "P2P": None}
    f = external_flows(None, cfg, 290.0, now_ms=9_999, read=lambda ex, since: found)
    assert f["start"] == 1_000 and round(f["net_out"], 2) == 190.5 and round(f["pnl"], 2) == 6.5
    found = {"提領": [], "入金": [], "Pay": [], "P2P": []}  # Binance's window moved on; the record is kept
    assert round(external_flows(None, cfg, 290.0, read=lambda ex, since: found)["net_out"], 2) == 190.5
    text = flows_text(f)
    assert "真實損益 +6.50 U" in text and "📤 淨轉出 190.50 U" in text and "P2P 紀錄讀取失敗" in text


class FakeFlows:
    def sapiGetCapitalWithdrawHistory(self, _p):
        return [{"id": "a", "coin": "USDT", "amount": "200", "transactionFee": "0.5", "status": 6},
                {"id": "b", "coin": "USDT", "amount": "50", "transactionFee": "0", "status": 1}]  # cancelled

    def sapiGetCapitalDepositHisrec(self, _p):
        return [{"id": "c", "coin": "BTC", "amount": "0.1", "status": 1}]

    def sapiGetPayTransactions(self, _p):
        return {"data": [{"transactionId": "r", "orderType": "SEND_MULTIPLE_V2", "currency": "USDT", "amount": "6", "transactionTime": 5},
                         {"transactionId": "k", "orderType": "CRYPTO_BOX", "currency": "USDT", "amount": "0.1", "transactionTime": 5},
                         {"transactionId": "s", "orderType": "C2C", "currency": "USDT", "amount": "-30", "transactionTime": 5}]}

    def sapiGetC2cOrderMatchListUserOrderHistory(self, p):
        if p["tradeType"] == "SELL":
            return {"data": [{"orderNumber": "x", "asset": "USDT", "amount": "20", "orderStatus": "COMPLETED"}]}
        return {"data": [{"orderNumber": "y", "asset": "USDT", "amount": "20", "orderStatus": "CANCELLED"}]}


def test_flow_rows_skip_rebates_red_packets_and_unfinished_orders():
    from cryptobot.money import _flow_rows
    assert _flow_rows(FakeFlows(), 0) == {"提領": [("wa", -200.5)], "入金": [], "Pay": [("ps", -30.0)], "P2P": [("cx", -20.0)]}


def test_rebate_line(tmp_path):
    cfg = {"goals": {"base_capital": 315.67}}
    lines = money_lines(None, cfg, equity=260.0, bot_total=-89.77, income=INCOME, predict=None, rebate=6.14)
    assert any(line.startswith("  🎁 返傭：+6.14 U") for line in lines)
