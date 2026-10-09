# automation-crypto — 全市場掃描合約交易機器人

預設：**每 4 小時掃描 Binance 成交量前 50 大 USDT 永續合約，找出正在突破的幣，順勢做多或做空**
5 倍逐倉・同時最多 4 個持倉・每個持倉風險 0.75%・交易所端停損・Telegram 通知

> ⚠️ **風險警告**：槓桿交易可能在短時間內虧光保證金。任何策略都可能虧損，回測績效不代表未來。
> 只投入「全部虧光也能接受」的金額，並先用模擬交易驗證。本專案不構成投資建議。

## 研究結論(Binance 合約 2024-01～2026-10 真實資料，已計入手續費、滑價、資金費率)

| 做法 | 結果 |
|---|---|
| 15m / 5m 當沖(4 種策略) | **全部虧損**，手續費吃掉約 80% 本金 |
| 預測漲跌方向(價格安靜、最近漲跌、追強勢幣) | **跟丟硬幣一樣**，沒有預測力 |
| 成交量暴增 | 之後 7 天大動(±15%)的機率從 27% 升到 45%，**但方向仍是一半一半** |
| 4h 突破，單一幣 | 12 種幣中 9 種賺錢；ETH +52%、BTC +14%、LTC -30% |
| **4h 突破，掃描 55 種幣(預設)** | **+95%，2024 +31% / 2025 +20% / 2026 +24%，最大回撤 -16.5%** |

另外發現：**突破時成交量越大，那筆交易平均賺越多**，所以多個幣同時出現訊號時，機器人優先進場成交量放大最多的。

> 注意：測試的 55 種幣是「現在還存在的主流幣」，已下市的幣不在裡面，實際表現可能比回測差一些。

## 策略 `breakout`：4 小時突破波段

- 收盤**突破前 40 根 4h K 線最高點 → 做多**；**跌破最低點 → 做空**
- 初始停損 3 倍 ATR；之後停損跟著最有利價格移動(4 倍 ATR)，**不設停利**，讓趨勢自己跑
- 勝率約 35%：常連續小賠幾筆，再靠少數大行情賺回來；持倉通常數天到數週

## 全市場掃描怎麼運作

1. 每天挑一次名單：24h 成交量前 50 大的 USDT 永續合約(排除穩定幣、成交量 < 2000 萬 USDT 的冷門幣)
2. 每根 4h K 線收盤(台灣時間 00/04/08/12/16/20 點)掃描名單上所有幣
3. 有突破訊號的幣依「成交量放大倍數」排序，空位有幾個就進場幾個(最多 4 倉、每種幣 1 倉)
4. 持倉期間每 5 秒檢查價格；交易所端也掛著停損單，伺服器斷線也有保護
5. 整個帳戶共用熔斷：從高點回撤 20% 停止開新倉；當日虧 6% 當天停止開新倉

**資金建議**：每倉風險 0.75%，300 USDT 時每倉約 2.25 USDT 風險。部分幣(如 BTC 最低下單 100 USDT)可能因金額太小被跳過，**建議 500 USDT 以上**。

## 激進版(`config.aggressive.yaml`)

`cp config.aggressive.yaml config.yaml` 即可使用：每個持倉風險 3%、停機線 70%、翻倍提醒、3000 U 自動停止開新倉。

| 312 U 起跳(2024-01～2026-10) | 保守版 | 激進版 |
|---|---|---|
| 34 個月後 | 約 480 U | 約 1,577 U |
| 中途最慘 | -19% | **-60%** |
| 最差單月 | -12% | **-41%** |

**提出本金的流程**(收到「帳戶翻倍」通知後)：
1. `sudo systemctl stop cryptobot` 停止機器人
2. 在 Binance 把本金從 U 本位合約劃轉出來
3. `python -m cryptobot --live reset-risk`(讓回撤從目前餘額重新計算，否則提領會被當成虧損)
4. `sudo systemctl start cryptobot` 重新啟動

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
# 1. 全市場掃描回測(55 種幣、1000 天，資料來自 Binance 公開歷史資料庫)
python -m cryptobot scan-backtest
python -m cryptobot scan-backtest --coins BTC,ETH,SOL,DOGE --trades

#    單一幣回測
python -m cryptobot --symbol ETH/USDT:USDT backtest --source vision

# 2. 參數最佳化：前 70% 資料找參數、後 30% 驗證，兩段都賺錢才會推薦
python -m cryptobot optimize

# 和舊策略比較
python -m cryptobot --strategy pullback --daytrade --timeframe 15m backtest

# 3. 測試 Telegram 通知
python -m cryptobot notify-test

# 4. 模擬交易：真實即時行情，假的錢(會發 Telegram 通知，標示 [模擬])
python -m cryptobot paper                           # 全市場掃描
python -m cryptobot --symbol ETH/USDT:USDT paper    # 只交易一種幣

# 5. 實盤前檢查(只讀取、不下單)：API 權限、保證金模式、餘額、行情資料
python -m cryptobot check

# 6. 小額實盤(真實資金！)
python -m cryptobot live --confirm-live
```

其他選項：`--symbol BTC/USDT:USDT`、`--timeframe 5m`、`--no-daytrade`(允許留倉過夜)、`--spot`(現貨只做多)、`-v`(顯示每筆交易細節)。

### 實盤常駐執行(systemd：開機自動啟動、當掉自動重啟)
```bash
sudo cp deploy/cryptobot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cryptobot

systemctl status cryptobot          # 狀態
journalctl -u cryptobot -f          # 即時 log(Ctrl+C 離開，不會停掉機器人)
sudo systemctl restart cryptobot    # 改完程式或設定後重啟
sudo systemctl stop cryptobot       # 安全停止(持倉的交易所停損單仍有效)
```
服務用系統的 `/usr/bin/python3`，套件請裝在系統層(`sudo pip install -r requirements.txt`)。

模擬交易(paper)可以用 tmux 在前景跑：`tmux new -s paper`，執行 `python -m cryptobot paper`，Ctrl+B 再按 D 離開。

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
  scanner.py     全市場掃描：挑幣、訊號排序、持倉上限、共用帳戶與熔斷、組合回測
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
