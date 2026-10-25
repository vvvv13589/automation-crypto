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


def test_rebates_count_unexplained_rises_of_the_spot_wallet(tmp_path):
    from cryptobot.money import rebates
    cfg = {"state_dir": str(tmp_path)}
    assert rebates(None, cfg, now_ms=1, spot=6.14) == 6.14            # first run: what is there
    assert rebates(None, cfg, now_ms=2, spot=10.14, flows=0.0) == 10.14  # +4 rebate
    assert rebates(None, cfg, now_ms=3, spot=0.14, flows=10.0) == 10.14  # moved 10 to futures: not income
    assert rebates(None, cfg, now_ms=4, spot=50.14, flows=-50.0) == 10.14  # a deposit: not income
    assert rebates(None, cfg, now_ms=5, spot=0.0, flows=50.0 - 0.14) == 10.14  # spent / moved: never negative


def test_rebate_line(tmp_path):
    cfg = {"goals": {"base_capital": 315.67}}
    lines = money_lines(None, cfg, equity=260.0, bot_total=-89.77, income=INCOME, predict=None, rebate=6.14)
    assert any(line.startswith("  🎁 返傭：+6.14 U") for line in lines)

def test_prediction_claims_since():
    from cryptobot.money import prediction_claims
    pages = {"/wallet/list": {"wallets": [{"walletAddress": "0xabc"}]},
             "/position/settled-history": {"positions": [
                 {"claimAmount": "33.79", "settledDate": 2000}, {"claimAmount": None, "settledDate": 2500},
                 {"claimAmount": "14.78", "settledDate": 1000}]}}
    assert prediction_claims(None, 1500, get=lambda path, **kw: pages[path]) == 33.79