# automation-crypto — 自適應合約交易機器人

預設設定：**Binance ETH/USDT 永續合約・5 倍逐倉・多空雙向・4 小時突破波段・每筆風險 2%・交易所端停損・Telegram 通知**

> ⚠️ **風險警告**：槓桿交易可能在短時間內虧光保證金。任何策略都可能虧損，回測績效不代表未來。
> 只投入「全部虧光也能接受」的金額，並先用回測與模擬交易驗證。本專案不構成投資建議。

## 研究結論(Binance 合約 2024-01～2026-10 真實資料，已計入手續費、滑價、資金費率)

| 策略 | 結果 |
|---|---|
| 15m / 5m 當沖(adaptive、pullback、突破、美股開盤區間突破) | **全部虧損**。扣成本前約打平，手續費吃掉約 80% 本金 |
| 美股開盤區間突破 | ETH 單一參數 +117%，但換參數或換 BTC/SOL 就虧 → 運氣，不是優勢 |
| **4h Donchian 突破波段(預設)** | **ETH/BTC/SOL 各 45 組參數，98%/100%/91% 獲利**；回撤約 -17%～-24% |

用預設參數(40 根突破、3 ATR 停損、4 ATR 移動停損)：ETH +52%、SOL +10%、BTC +3%(2.7 年)。
報酬溫和、會連虧好幾筆再靠少數大趨勢賺回來(勝率約 35%)；強烈單邊牛市會明顯落後買入持有。

## 預設策略 `breakout`：4 小時突破波段

- 收盤**突破前 40 根 4h K 線最高點 → 做多**；**跌破最低點 → 做空**
- 初始停損 3 倍 ATR；之後停損跟著最有利價格移動(4 倍 ATR)，**不設停利**，讓趨勢自己跑
- 持倉時間通常數天到數週；每月約 3 筆交易，手續費很低

## 其他策略(研究對照用，`--strategy pullback|adaptive`)

### `pullback`：大週期順勢 + 回檔進場

1. **4 小時線定方向**：EMA20 > EMA50 且收盤在 EMA50 之上 → 只做多；相反 → 只做空；不明確 → 不交易
2. **15 分鐘線等回檔**：做多時，等 RSI 先跌破 40(回檔)，再重新站上 50、收盤在 EMA20 之上(回檔結束)才進場；做空相反
3. **停損**在回檔低點外(1～3 倍 ATR，回檔太深就不做)；**停利** 2 倍風險；賺到 1 倍風險時停損移到**成本價**
4. 4 小時趨勢反轉時提早出場

### 對照組 `adaptive`：盤勢切換(用 `--strategy adaptive`)

| 市場狀態 | 判斷 | 做多 | 做空 |
|---|---|---|---|
| **趨勢盤** | ADX ≥ 25 且上升中 | EMA12 > EMA26、價格在 EMA200 上、+DI > -DI | 完全相反 |
| **盤整盤** | ADX ≤ 20 | 跌破布林下軌 + RSI < 30 | 突破布林上軌 + RSI > 70 |

> 兩者在 15 分鐘當沖上都虧損，不建議實盤使用。

**當沖模式**(`--daytrade`)：每天台灣時間 06:45 後不開新倉，07:45 強制全部平倉。

### 下單與風控
- 進出場預設市價(可改成 post-only 限價進場)；停損一律市價，確保出得去
- **交易所端停損單**：每次開倉同步在 Binance 掛停損單(移動停損時跟著更新)。就算伺服器斷線、程式當掉，停損仍然有效
- 每筆停損最多虧總資金 **2%**（依停損距離計算部位大小，槓桿只決定保證金用量）
- 部位名目價值最多 5 倍總資金；逐倉模式，單筆爆倉不會拖累帳戶其他資金
- 當日虧 6% 停止開新倉；從高點回撤 20% 完全停機（刪除 `state/` 檔案才恢復）
- 當沖模式下每天最多開 6 次倉
- 回測已計入：maker/taker 手續費、滑價、資金費率(保守估計，多空都扣)、限價單沒成交的情況

## 安裝

```bash
cd ~/automation-crypto
git pull
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
cp .env.example .env        # 填入 Telegram / API key
```

## 使用流程

```bash
# 1. 回測(預設 1000 天；--source vision 用 Binance 公開歷史資料庫，API 被封鎖的地區也能用)
python -m cryptobot backtest --trades
python -m cryptobot --symbol BTC/USDT:USDT backtest --source vision

# 2. 參數最佳化：前 70% 資料找參數、後 30% 驗證，兩段都賺錢才會推薦
python -m cryptobot optimize

# 和舊策略比較
python -m cryptobot --strategy pullback --daytrade --timeframe 15m backtest

# 3. 測試 Telegram 通知
python -m cryptobot notify-test

# 4. 模擬交易：真實即時行情，假的錢(會發 Telegram 通知，標示 [模擬])
python -m cryptobot paper

# 5. 小額實盤(真實資金！)
python -m cryptobot live --confirm-live
```

其他選項：`--symbol BTC/USDT:USDT`、`--timeframe 5m`、`--no-daytrade`(允許留倉過夜)、`--spot`(現貨只做多)、`-v`(顯示每筆交易細節)。

### 背景執行(關掉 SSH 也繼續跑)
```bash
tmux new -s bot
source .venv/bin/activate && python -m cryptobot paper
# 按 Ctrl+B 再按 D 離開；tmux attach -t bot 回來；Ctrl+C 安全停止
```

### 實盤前檢查清單
1. Binance API key：**只開「合約交易」權限，關閉提領**，綁定伺服器 IP
2. 合約帳戶只轉入要給機器人的金額（建議先 100 USDT）
3. 機器人使用**單向持倉模式**，啟動時會自動設定 5 倍、逐倉
4. 不要在同一個帳戶手動交易 ETH 合約(機器人只管自己開的倉)
5. 先讓 `paper` 跑幾天，確認 Telegram 通知與行為符合預期

## 專案結構

```
cryptobot/
  strategy.py    breakout(4h 突破，預設)、pullback(大週期順勢+回檔)、adaptive(盤勢切換)
  trader.py      交易核心(回測/模擬/實盤共用)：限價進場、停損、移動停損、當沖時段、資金費率
  broker.py      PaperBroker(模擬保證金帳戶) / FuturesLiveBroker(Binance 合約) / SpotLiveBroker
  risk.py        部位大小、每日虧損 / 最大回撤熔斷
  backtest.py    回測與績效統計
  optimize.py    參數搜尋 + 樣本外驗證
  runner.py      即時交易迴圈(斷線自動重試、與交易所持倉同步)
  notify.py      Telegram 通知
  indicators.py  EMA、RSI、ATR、ADX、布林通道
  data.py        K 線下載 / CSV / 模擬行情
tests/           單元測試(含多空、當沖換日、資金費率、無未來函數檢查)
```

執行測試：`python -m pytest -q`
