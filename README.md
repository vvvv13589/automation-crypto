# automation-crypto — 自適應虛擬貨幣自動交易機器人

會**依照市場狀態即時切換策略**的現貨自動交易程式，透過 [ccxt](https://github.com/ccxt/ccxt) 支援 Binance、OKX、Bybit、Kraken 等上百家交易所。

> ⚠️ **風險警告**：加密貨幣波動極大，任何策略都可能虧損，回測績效不代表未來報酬。
> 請先用 `backtest` 與 `paper`(模擬)模式長時間驗證，再考慮小額實盤。本專案不構成投資建議。

## 運作方式

```
每 10 秒 ──► 取得最新成交價 ──► 檢查停損 / 移動停損 / 停利（即時保護）
每根 K 線收盤 ──► 計算指標 ──► 判斷市場狀態 ──► 決定進出場
```

| 市場狀態 | 判斷方式 | 使用策略 |
|---|---|---|
| **趨勢盤** | ADX ≥ 25 | 順勢：EMA12 > EMA26、價格在 EMA200 之上、+DI > -DI 時買進；以 3×ATR 移動停損抱住趨勢，EMA 死叉或空方 DI 轉強時出場 |
| **盤整盤** | ADX ≤ 20 | 均值回歸：價格跌破布林下軌且 RSI < 30 時買進；回到布林中軌停利 |
| **不明確** | 20 < ADX < 25 | 不開新倉，只管理現有部位 |

持倉中若市場改變也會跟著調整：盤整單遇到向上突破會**升級為趨勢單**（取消停利、改用移動停損），盤整單遇到空頭趨勢則提早出場。

### 風險控管
- **每筆風險固定**：依 ATR 停損距離計算部位，停損出場最多虧損總資產 1%
- **單一部位上限**：最多佔總資產 50%
- **每日虧損上限**：當日虧 5% 停止開新倉，隔日恢復
- **最大回撤熔斷**：從高點回撤 20% 完全停止開新倉（需刪除 state 檔才會恢復）
- **冷卻期**：平倉後等 3 根 K 線才再進場，避免來回洗盤
- 只做**現貨多單**，不開槓桿、不放空
- 持倉狀態寫入 `state/`，程式重啟後可接續管理部位；每筆交易記錄在 `logs/*_trades.csv`

## 安裝

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 依需求修改交易所、幣種、週期、風控參數
```

## 使用

```bash
# 1. 回測（從交易所下載最近 90 天資料）
python -m cryptobot backtest --days 90
python -m cryptobot --symbol ETH/USDT --timeframe 1h backtest --days 180 --trades
python -m cryptobot backtest --days 365 --refresh   # 下載的資料會快取在 data/，--refresh 重新下載
python -m cryptobot backtest --synthetic                        # 離線用模擬行情測試

# 參數最佳化：前 70% 資料找參數，後 30% 驗證(避免過度擬合)
python -m cryptobot optimize                       # 測 1h、4h，一年資料
python -m cryptobot --daytrade optimize            # 當沖：測 5m、15m

# 當沖模式回測(台灣時間每天 23:45 強制平倉)
python -m cryptobot --daytrade --timeframe 15m backtest --days 90

# 2. 模擬交易：使用真實即時行情，但不會真的下單
python -m cryptobot paper

# 3. 實盤交易（真實資金！）
cp .env.example .env    # 填入 API key
python -m cryptobot live --confirm-live
```

### 實盤前的安全清單
1. API key **只開「交易」權限，關閉「提領」**，並設定 IP 白名單
2. 先在交易所測試網跑（`config.yaml` 設 `exchange.sandbox: true`，需使用測試網 key）
3. `paper` 模式至少跑數週，確認行為符合預期
4. 用一個只放少量資金的子帳戶開始
5. 機器人只管理**自己開的部位**；請勿在同一帳戶同一幣種手動交易
6. 建議放在 VPS 上用 `systemd`/`tmux`/`docker` 長時間執行，按 `Ctrl+C` 會安全停止

## 專案結構

```
cryptobot/
  indicators.py  EMA、RSI、ATR、ADX、布林通道
  strategy.py    市場狀態判斷 + 自適應進出場邏輯
  risk.py        部位大小計算、每日虧損 / 最大回撤熔斷
  broker.py      PaperBroker(模擬撮合，含手續費與滑價) / LiveBroker(ccxt 真實下單)
  trader.py      交易核心：回測、模擬、實盤共用同一套邏輯
  backtest.py    回測與績效統計(報酬、最大回撤、Sharpe、勝率、獲利因子)
  runner.py      即時交易迴圈(斷線自動重試)
  data.py        K 線下載 / CSV / 模擬行情
tests/           單元測試(含「無未來函數」檢查)
```

執行測試：`python -m pytest -q`

## 調整策略

所有參數都在 `config.yaml` 的 `strategy` 與 `risk` 區塊。建議做法：改一個參數 → 在**多個幣種、多段時間**回測 → 比較「總報酬 vs 買入持有」與「最大回撤」，避免針對單一歷史區間過度最佳化。
