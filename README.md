# automation-crypto — 自適應合約當沖機器人

會**依照市場狀態即時切換策略**的虛擬貨幣自動交易程式。預設設定：

**Binance ETH/USDT 永續合約・5 倍逐倉・多空雙向・當沖(台灣時間 07:45 全部平倉)・每筆風險 2%・Telegram 通知**

> ⚠️ **風險警告**：槓桿交易可能在短時間內虧光保證金。任何策略都可能虧損，回測績效不代表未來。
> 只投入「全部虧光也能接受」的金額，並先用回測與模擬交易驗證。本專案不構成投資建議。

## 運作方式

```
每 5 秒 ──► 最新成交價 ──► 停損 / 移動停損 / 停利 / 限價單是否成交 / 當沖時間到了沒
每根 K 線收盤 ──► 計算指標 ──► 判斷市場狀態 ──► 做多 / 做空 / 出場
```

| 市場狀態 | 判斷 | 做多 | 做空 |
|---|---|---|---|
| **趨勢盤** | ADX ≥ 25 且上升中 | EMA12 > EMA26、價格在 EMA200 上、+DI > -DI | 完全相反 |
| **盤整盤** | ADX ≤ 20 | 跌破布林下軌 + RSI < 30 (順 EMA200 方向) | 突破布林上軌 + RSI > 70 |
| **不明確** | 介於兩者 | 不開新倉，只管理持倉 | |

- 趨勢單：用 ATR 移動停損抱住行情，EMA 反向交叉時出場
- 盤整單：回到布林中軌停利；若行情變成同方向趨勢 → 升級為趨勢單繼續抱
- **當沖**：每天台灣時間 06:45 後不開新倉，07:45 強制全部平倉，不留倉過夜

### 下單與風控
- **進場**掛 post-only 限價單(maker 0.02%)，一根 K 線沒成交就取消；**出場**一律市價，確保出得去
- **交易所端停損單**：每次開倉同步在 Binance 掛停損單(移動停損時跟著更新)。就算伺服器斷線、程式當掉，停損仍然有效
- 每筆停損最多虧總資金 **2%**（依停損距離計算部位大小，槓桿只決定保證金用量）
- 部位名目價值最多 5 倍總資金；逐倉模式，單筆爆倉不會拖累帳戶其他資金
- 當日虧 6% 停止開新倉；從高點回撤 20% 完全停機（刪除 `state/` 檔案才恢復）
- 每天最多開 6 次倉；平倉後冷卻 2 根 K 線
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
# 1. 回測(會下載 Binance 合約歷史資料，快取在 data/)
python -m cryptobot backtest --days 90 --trades

# 2. 參數最佳化：前 70% 資料找參數、後 30% 驗證(避免過度擬合)，當沖預設測 5m、15m
python -m cryptobot optimize --days 120

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
  strategy.py    市場狀態判斷 + 多空自適應進出場
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
