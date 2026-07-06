# 囤貨偵測系統 — 三階段執行計劃

> 本文件為交辦包，設計為可獨立執行：執行者不需要本計劃以外的對話脈絡。
> 三個 Phase 依序執行，每個 Phase 完成並通過驗收後才進下一個。
> 執行前先讀「§0 共同背景」，它適用於所有 Phase。

---

## §0 共同背景（所有 Phase 必讀）

### 專案概述
台股券商分點大額買進監控系統。單一 Python 檔 `broker_monitor.py`（約 1550 行，無第三方依賴，
只用 stdlib + `curl`/`iconv` subprocess）。每日由 GitHub Actions（台灣 17:30 觸發）執行：
抓 10 個分點的當日買超榜 → 存快照 → 計算信號 → 產生 `index.html`（GitHub Pages 直接 serve
main 分支）→ 寄 email 日報。

- Repo：`/Users/mou/broker-monitor`（GitHub: or-mouuu/broker-monitor，main 分支）
- 網頁：https://or-mouuu.github.io/broker-monitor/
- 本機驗證指令：`python3 broker_monitor.py`（產生 index.html，不寄信）

### 資料結構（關鍵）

**每日快照** `data/YYYYMMDD.json`（目前約 15 份，上限 `MAX_HISTORY = 20` 天）：
```json
{
  "date": "20260703",
  "branches": {
    "元大-崇德": [
      {"ticker": "2368", "name": "金像電", "buy": 85654, "sell": 369, "net": 85285},
      ...最多 50 檔（該分點當日買超榜前 50，金額單位＝千元）
    ],
    ...共 10 個分點
  }
}
```
⚠️ 重要限制：快照只含**買超榜**。分點賣超某股票時該股不會出現，因此無法得知賣出量，
所有「持股估計」都是上限估計。文案中要用「估計吃貨」而非「持股」。

**TWSE 價量**（記憶體內，執行期抓取，不落地）：
- `fetch_twse_volumes(tickers, date_str)`（broker_monitor.py:366）回傳三個 dict：
  - `twse_vol = {ticker: 當日成交金額千元}`
  - `twse_monthly = {ticker: {yyyymmdd: 成交金額千元}}` — 僅**當月**資料（TWSE STOCK_DAY API 一次回一個月）
  - `twse_ohlc = {ticker: {yyyymmdd: {"o","h","l","c"}}}` — 同樣僅當月
- ⚠️ 月初時 `twse_monthly`/`twse_ohlc` 只有 1~3 天資料。若指標需要跨月價量，
  必須擴充 `_fetch_one_volume`（broker_monitor.py:335）多抓上個月（date 參數改上月任一天再呼叫一次 API，合併 dict）。
  Phase 1 需要 14 個交易日的價量 → **必須做跨月抓取**（見 Phase 1 步驟 2）。
- 上櫃股票（TPEx）此 API 無資料 → dict 中缺 key，程式已有的慣例是缺資料時降級顯示 "–"，不可 crash。

### 既有函數地圖（行號為 2026-07-03 版本，會漂移，以函數名為準）
| 函數 | 行號 | 用途 |
|---|---|---|
| `load_history()` | 410 | 讀 `data/*.json` 最新 MAX_HISTORY 份，回傳 list[dict]（新→舊） |
| `get_accumulation_data()` | 426 | 既有 7 日積累窗口計算（勿改動，囤貨是獨立新指標） |
| `_get_recent_ohlc(ticker, upto_date, twse_ohlc, n)` | 551 | 取最近 n 個交易日 OHLC（舊→新） |
| `build_consensus()` | 622 | 跨分點共識，回傳 list[dict]，含 `ticker/branches/total_net` 等 |
| `make_candle_svg(ohlc_list)` | 519 | 5 日迷你 K 線 SVG |
| `render_html(all_branches, consensus, twse_vol, twse_monthly, twse_ohlc)` | 1148 | 整頁 HTML，含 tabs、stats 卡、modal |
| `render_branch_section()` | 983 | 單一分點的表格（含排序 `sortTable`、`col-hide` 響應式欄） |
| `render_email_digest()` | 1229 | email 日報 HTML（inline style，勿用外部 CSS class） |
| `send_email(all_branches, consensus, twse_ohlc, data_date)` | 1420 | 組日報並寄出 |
| `main()` | 1444 | 主流程；`--email` 旗標控制寄信 |

### 主流程順序（main() 內，新步驟要插對位置）
```
load_history → fetch 各分點 → apply_accumulation → save_daily_snapshot
→ fetch_twse_volumes → apply_strong_accum → build_consensus
→ render_html → 寫 index.html → send_email
```

### 程式風格約定
- 繁體中文註解與 print 訊息；區段用 `# ═══...` 分隔線
- 常數大寫置頂（33-40 行附近），新常數加在該區並附中文註解
- HTML 產出用 f-string 拼接；**f-string 表達式內不可含反斜線**（CI 用 Python 3.11，會 SyntaxError——已踩過雷）
- 金額千元用 `fmt_n()` 格式化；紅漲綠跌（台灣慣例）
- 網頁 CSS 在 `CSS` 常數（681 行附近）、JS 在 `JS` 常數；email 一律 inline style

### Git 約定
- 直接在 main 工作。push 前先 `git fetch origin && git rebase origin/main`
  （GitHub Actions 每日會自動 commit `index.html` + `data/`，經常造成 non-fast-forward）。
- rebase 衝突多半在 `index.html`：它是產物，解法＝取任一邊後重跑 `python3 broker_monitor.py` 再 commit。
- **禁止 `git reset --hard`**（曾因此弄丟未推送的 commit）。
- commit 訊息格式：`feat:`/`fix:` + 中文描述。

### 回報合約（一字不改）
> 回報限制：只回（1）結論 ≤10 行（2）證據＝檔案:行號或指令輸出關鍵行（3）你改過／建立的檔案清單。
> 長產物（報告、大 diff、掃描結果）存到指定路徑，回報只給路徑。禁止把讀到的檔案原文貼回。

---

## Phase 1 — 囤貨分數引擎 + 網頁「囤貨追蹤」tab

**建議模型**：中階檔（sonnet 級）。**預估工作量**：一個 session 可完成。

### 目標與動機
對每個「分點 × 個股」配對計算 0~100 的囤貨分數，偵測「分點正在悄悄吸籌某檔股票」。
與既有 7 日「積累」指標的差異：窗口更長（14 交易日）、加入價格行為維度（逆勢買、買不動價、
成本乖離），目的是在起漲前找到主力吃貨標的。

### 規格

#### 1. 新常數（加在 STRONG_ACCUM 常數區之後）
```python
HOARD_WINDOW    = 14    # 囤貨觀察窗口（交易日）
HOARD_MIN_DAYS  = 6     # 窗口內至少買超天數（低於此不計分）
HOARD_MIN_TOT   = 30_000  # 窗口累積買超下限（千元），過濾雜訊
HOARD_SCORE_MIN = 60    # 進榜門檻
```

#### 2. 擴充跨月價量抓取
修改 `_fetch_one_volume`（broker_monitor.py:335）：若當前日期的「日」< 20（表示本月資料
不足 14 個交易日），額外用上個月任一日期再呼叫一次 TWSE STOCK_DAY API，把兩個月的
`monthly` 與 `monthly_ohlc` 合併回傳。注意：
- API URL 格式見常數 `TWSE_API`；date 參數為 yyyymmdd，回傳該月整月
- 並發已由 `fetch_twse_volumes` 的 ThreadPoolExecutor 處理，單檔多抓一次即可，勿另開線程池
- 加上限流保護：兩次呼叫間 `time.sleep(0.1)` 以内即可（既有程式無 sleep，維持輕量）

#### 3. 囤貨分數計算 `compute_hoarding(all_branches, history, twse_monthly, twse_ohlc, data_date)`
對每個分點的每檔股票（含今日與歷史快照中出現過的）：

先組出該配對在近 `HOARD_WINDOW` 個交易日的序列：
- 交易日集合：以該股在 `twse_ohlc` 中有價格的日期為準（≤ data_date，取最近 14 個）
- `daily_net[d]`：該分點該股當日 net（從 history 快照撈；不在買超榜的日子視為 0）
- 前置過濾：買超天數 < `HOARD_MIN_DAYS` 或 累積買超 < `HOARD_MIN_TOT` → 跳過

五個維度（總分 100）：
| 維度 | 計算 | 滿分條件 | 配分 |
|---|---|---|---|
| persistence | 買超天數 / 14 | ≥ 0.6 | 25 |
| dip_buying | 股價收跌日中有買超的比例（收跌日 = close < 前日 close） | ≥ 0.5；若窗口內收跌日 <3 天，此維度按比例縮小配分並將剩餘配分攤給 persistence | 25 |
| stealth | 累積買超佔市場量 pct 排該分點所有配對前 30% 且 期間漲幅 < 8% | 漲幅 <5% 給滿分，5~8% 線性遞減，>15% 給 0 | 20 |
| absorption | 累積買超 ÷ 期間市場成交金額 | ≥ 1.0% 滿分，線性遞減至 0.2% 為 0 | 20 |
| cost_edge | 現價 ÷ 估計成本 - 1 | 乖離 ∈ [-3%, +5%] 滿分，+5%~+15% 線性遞減，>+15% 為 0（漲太多）；<-3% 也遞減（跌破成本是警訊） | 10 |

估計值：
- `est_shares`（估計吃貨張數）= Σ(daily_net[d] ÷ close[d])，單位換算注意：net 是千元、close 是元 → 張數 = net*1000/close/1000 = net/close（千股=張）
- `est_cost`（估計成本價）= Σ(daily_net) ÷ est_shares

回傳 list[dict]，每項含：`branch, ticker, name, score, buy_days, window_days, total_net,
est_shares, est_cost, close, cost_dev_pct, dims{五維各自得分}`，
依 score 降冪排序，只保留 score ≥ `HOARD_SCORE_MIN`。
同一 ticker 被多分點囤貨時每個配對獨立成列，另加欄位 `co_hoard_count`（該 ticker 進榜的分點數）。

TPEx（無價格資料）處理：dip_buying / stealth / cost_edge 三維無法算 → 這三維得 0、
其餘兩維照算，score 上限自然只剩 45，通常不進榜；在 dict 加 `no_price: True` 供 UI 標注。

#### 4. 網頁 UI
- `render_html` 的 tabs 列，在「🔗 共識買進」之後插入「🎯 囤貨追蹤」tab
- panel 內容：一個可排序表格（沿用既有 `sortTable`/`col-hide` 機制），欄位：
  `# | 代號/名稱 | 分點 | 囤貨分數 | 買超天數 | 估計吃貨(張) | 估計成本 | 現價(乖離%) | 收盤/K線`
  - 囤貨分數欄：≥80 顯示紅底 pill「強」、60~79 橙底 pill；hover title 顯示五維分數明細
  - `co_hoard_count ≥ 2` 的 ticker 在名稱旁加 pill「N點共囤」（最強訊號，藍底）
  - K 線直接複用 `make_candle_svg` + `_get_recent_ohlc`
- stats 卡片區加一張「囤貨追蹤 N」卡，點擊 `showTab` 切到新 tab（參考既有共識買進卡寫法）
- 說明文字（hint）：一句話解釋囤貨分數定義與資料限制（估計值、僅買超榜）

#### 5. main() 接線
`compute_hoarding` 需要 history 與 twse 資料 → 在 `apply_strong_accum` 之後呼叫，
結果傳入 `render_html`（簽名加參數 `hoarding: list[dict]`）。

### 驗收條件
1. `python3 -c "import broker_monitor"` 無錯誤（特別注意 f-string 反斜線）
2. `python3 broker_monitor.py` 跑完產生 index.html，console 印出「🎯 囤貨追蹤：N 筆」摘要（前 5 名：分點|代號|名稱|分數|估計成本 vs 現價）
3. index.html 中存在「囤貨追蹤」tab 且表格有資料列（用 grep 驗證），各欄可排序（data-v 屬性齊全）
4. 手工抽查 2 筆進榜配對：用 `data/*.json` 人工核算買超天數與累積金額，與表格一致
5. 月初情境不壞：模擬 `twse_monthly` 只有 3 天資料時（可用單元式小腳本注入），程式不 crash、分數降級計算
6. 既有功能不壞：共識買進 tab、各分點 tab、email digest 照常產出（grep 既有關鍵字仍在）

### 邊界
- 只改 `broker_monitor.py`；不動 `data/`、workflow、branches.json
- 不修改既有 `get_accumulation_data` / `apply_strong_accum` 的邏輯
- email 部分本 Phase 不動（Phase 2 才做）
- 要動範圍外先停下回報
+ 回報合約（見 §0）

---

## Phase 2 — Email 囤貨警示 + 新進榜偵測

**建議模型**：中階檔（sonnet 級）。**前置**：Phase 1 已合併且驗收通過。

### 目標與動機
每日 email 日報加入「🎯 囤貨警示」節；並偵測「新進榜」（昨天未達標、今天達標的配對）——
新進榜是實戰上最值得看的訊號（吃貨行為剛成形）。

### 規格

#### 1. 分數快照持久化
- 新檔案 `data/hoard_scores.json`（單一檔案，滾動保留最近 5 個交易日）：
```json
{
  "20260703": [ {"branch": "...", "ticker": "...", "score": 72}, ... ],
  "20260702": [ ... ]
}
```
- main() 中 compute_hoarding 之後寫入；超過 5 天的 key 刪除
- GitHub Actions workflow 的 commit step 已含 `git add data/`，此檔會自動被版控，無需改 workflow

#### 2. 新進榜偵測
- `hoard_scores.json` 中找**最近一個早於今日的 key**作為「昨日」（注意假日跳空，不能用日曆天-1）
- 今日進榜且（昨日不存在該 branch+ticker 配對 或 昨日 score < HOARD_SCORE_MIN）→ `is_new: True`
- 首次執行（檔案不存在）：全部視為非新進榜（避免第一天全部標新）

#### 3. Email 日報改版
`render_email_digest`（broker_monitor.py:1229）：
- 摘要欄加「🎯 囤貨 N」
- 在「🔗 共識買進」之後插入「🎯 囤貨警示」節（位置提前，因為這是最核心的推薦）：
  - 最多 10 列：代號/名稱、分點、分數、估計成本 vs 現價乖離、`is_new` 標「NEW」pill（紅底白字）
  - `co_hoard_count ≥ 2` 標「N點共囤」pill
  - 排序：is_new 優先，再按 score 降冪
- email 一律 inline style，禁止外部 CSS；風格對齊既有各節（TH/TD/TBL 變數已定義在函數內）
- `send_email` 與 `render_email_digest` 簽名補傳 hoarding 結果

### 驗收條件
1. 本機跑兩次 `python3 broker_monitor.py`（第二次前手動把 hoard_scores.json 的今日 key 改名成前一交易日）→ 第二次執行 console 印出新進榜偵測結果，驗證 is_new 邏輯（含假日跳空情境的說明）
2. `data/hoard_scores.json` 產生且格式正確、只保留 ≤5 個 key
3. 把 `render_email_digest` 輸出寫到 `/tmp/digest_test.html` 用瀏覽器目視：囤貨節在共識買進之後、NEW pill 正確顯示（回報附截圖路徑或 HTML 路徑）
4. EMAIL 環境變數未設定時照常跳過寄信不 crash
5. 既有 email 各節（共識/連買/強積累/爆量）仍存在

### 邊界
- 可改：`broker_monitor.py`、新增 `data/hoard_scores.json`
- 不動 workflow、不動網頁 UI（Phase 1 已完成的部分只接資料不重寫）
+ 回報合約（見 §0）

---

## Phase 3 —（可選）TPEx 上櫃價量補完

**建議模型**：中階檔。**前置**：Phase 1、2 上線至少 3 個交易日、確認囤貨榜運作正常後再做。
**動機**：目前上櫃股票無價格資料，囤貨分數三個維度缺失，幾乎不可能進榜，形成盲區。
分點買超榜中上櫃股票比例不低（觀察值：約 2~3 成），值得補。

### 規格

#### 1. TPEx API 調研（先做，結論寫進實作）
候選端點（以實測為準，TPEx 網站改版頻繁）：
- `https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock?code={ticker}&date={yyyy/mm/dd}&response=json`（個股日成交資訊，月為單位）
- 舊版：`https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php?d={roc_date}&stkno={ticker}`
- 驗收前置：先用 curl 實測 2 檔上櫃股票（例：5274 信驊、3105 穩懋——如已上市則另找），
  確認回傳欄位對應（日期/成交金額/OHLC），把實測 request/response 樣本存到 `docs/tpex_api_sample.md`
- ⚠️ TPEx 日期多用民國年；金額單位可能是「元」或「千元」，必須用已知股票核對數量級

#### 2. 實作
- 新函數 `_fetch_one_tpex(ticker, date_str)`，回傳格式與 `_fetch_one_volume` 完全一致
  `(ticker, today_val, monthly, monthly_ohlc)`，金額統一換算為千元
- 路由邏輯：`fetch_twse_volumes` 中先打 TWSE，失敗或空資料的 ticker 再打 TPEx
  （或維護一個 in-memory set 記住已知上櫃代號，減少重複打錯 API）
- 跨月邏輯與 TWSE 版一致（Phase 1 步驟 2 的模式）
- 失敗容忍：TPEx 也查無 → 維持現狀（降級計分、UI 顯示 "–"），不 crash
- console 摘要印「TWSE 成功 X 檔、TPEx 補抓成功 Y 檔、無資料 Z 檔」

### 驗收條件
1. `docs/tpex_api_sample.md` 存在，含實測樣本與欄位對應說明
2. 挑一檔確定在分點買超榜出現過的上櫃股票：跑完後它在囤貨榜/分點表格有收盤價與 K 線（此前是 "–"）
3. 金額數量級核對：該上櫃股票的「佔市場量%」數值合理（0.01%~10% 區間，非 100 倍錯位）
4. TWSE 上市股票行為完全不變（抽 2 檔比對改動前後的佔市場量與收盤價一致）
5. 全流程執行時間不失控：console 記錄總耗時，比改動前增加 < 60 秒

### 邊界
- 可改：`broker_monitor.py`、新增 `docs/tpex_api_sample.md`
- API 若全部實測失敗（TPEx 改版擋爬蟲等），停下回報，附實測記錄，不要硬繞（不做 headless browser 之類的重手段）
+ 回報合約（見 §0）

---

## Phase 4 — 新鮮度維度（行為改變偵測）

**建議模型**：中階檔（sonnet 級）。
**前置**：本地快照累積 ≥30 個交易日後再執行（`data/*.json` 檔數 ≥30，
撰寫本節時為 2026-07-06、僅約 16 份；預估 2026-07-20 之後足夠）。
**背景**：2026-07-06 已完成「P1 相對化門檻」（commit 8d533e4）：進榜需期間吃貨佔市場量
≥ `HOARD_MIN_VOL_PCT`(0.5%)，隱蔽性排名改用佔比。榜單已從大型股例行流量轉為有感吸籌。
本 Phase 是下一步：區分「例行流量」與「行為改變」。

### 目標與動機
專業籌碼分析裡最強的訊號是**行為改變**：一個平常不碰某檔股票的分點，突然開始連續吃貨。
反之，一個分點 30 天內天天買同一檔（如法人渠道對權值股的經紀流量），即使佔比達標，
也只是常態流量而非新佈局。目前五維計分無法區分這兩者——「持續性」甚至獎勵後者。
本 Phase 加入「新鮮度」訊號：近期熱、前期冷 → 加分；前期也一樣熱 → 標記為常態流量。

### 規格

#### 1. 新常數（加在 HOARD 常數區）
```python
FRESH_PRIOR_WINDOW  = 16    # 前期比較窗口（緊接在 14 日主窗口之前的交易日數）
FRESH_HOT_RATIO     = 2.0   # 近期買超頻率 ÷ 前期頻率 ≥ 2 → 新鮮
FRESH_ROUTINE_FREQ  = 0.7   # 前期買超頻率 ≥ 70% → 常態流量
```

#### 2. `compute_hoarding` 內新增新鮮度計算
對每筆已通過既有門檻的配對（在組裝 `results` 的迴圈內）：
- 取主窗口（`trading_days`，14 日）**之前**的 `FRESH_PRIOR_WINDOW` 個交易日
  （用同一份價格日曆 `_hoard_window_dates` 的邏輯往前延伸；價格日曆不足時用本地快照日期）
- `prior_freq`  = 前期買超天數 / 前期實際可比對天數
- `recent_freq` = 主窗口買超天數 / 主窗口天數（既有 `buy_days / len(trading_days)`）
- 分類（互斥，寫入結果 dict）：
  - `freshness: "new"`     — prior_freq < 0.2 且 recent_freq ≥ 0.4（前期幾乎不碰、近期高頻）
  - `freshness: "rising"`  — recent_freq / max(prior_freq, 0.05) ≥ FRESH_HOT_RATIO
  - `freshness: "routine"` — prior_freq ≥ FRESH_ROUTINE_FREQ（前期就天天買）
  - `freshness: "steady"`  — 其餘
- **降級規則**：前期可比對天數 < 10（快照不足）→ `freshness: "unknown"`，不加不減分
- 計分調整（修改總分，不新增第六維、維持 100 上限）：
  - `new`：總分 ×1.10（上限 100）
  - `rising`：總分 ×1.05（上限 100）
  - `routine`：總分 ×0.85
  - `steady` / `unknown`：不變
  - 調整後再套 `HOARD_SCORE_MIN` 門檻（即 routine 可能因此掉出榜）

#### 3. UI 與 email
- 網頁 `render_hoarding_table`：`new` → 紅底 pill「新進場」；`rising` → 橙底 pill「升溫」；
  `routine` → 灰底 pill「常態流量」（仍顯示但視覺降權）；`steady`/`unknown` 不加 pill
- hint 補一句新鮮度說明
- email `render_email_digest` 囤貨警示節：`new`/`rising` 加同語意 pill（inline style）；
  排序改為：is_new（快照比對）優先 → freshness=="new" 次之 → score 降冪

#### 4. 與既有 `is_new`（Phase 2 快照比對）的關係
兩者語意不同，都保留：`is_new` = 昨天沒進榜今天進榜（榜單層面）；
`freshness` = 買超行為本身的頻率變化（行為層面）。UI 上 NEW pill 與新進場 pill 可並存。

### 驗收條件
1. `python3 -c "import broker_monitor"` 無錯誤
2. 快照 ≥30 份時：console 摘要印出 freshness 分佈（new/rising/steady/routine/unknown 各幾筆）
3. 手工抽查：挑 1 筆 `routine`，用 `data/*.json` 核算其前期買超頻率確實 ≥70%；
   挑 1 筆 `new`，核算前期頻率 <20%
4. 快照不足情境：暫時把 history 截斷到 20 份重跑 → 全部 `unknown`、分數與未實作前一致、不 crash
5. 既有功能不壞：囤貨 tab、email 囤貨警示、hoard_scores.json 照常運作

### 邊界
- 只改 `broker_monitor.py`
- 不動 P1 的相對化門檻邏輯（HOARD_MIN_VOL_PCT 等）
- 不動既有五維的個別計分公式，只在總分層做乘數調整
+ 回報合約（見 §0）

---

## 附錄：常見坑（來自本專案實際踩雷）
1. **f-string 內反斜線** → CI Python 3.11 直接 SyntaxError。條件式 HTML 先存變數再放進 f-string。
2. **push 被拒** → Actions 每日自動 commit。永遠 `fetch + rebase`，禁止 `reset --hard`。
3. **index.html 衝突** → 它是產物：任取一邊 → 重跑腳本 → 再 commit。
4. **月初 TWSE 資料不足** → STOCK_DAY API 只回當月，跨月要抓兩次。
5. **快照只有買超榜** → 任何「持股/成本」都是估計上限，文案與變數命名用 `est_` 前綴。
6. **民國年** → TWSE/TPEx 回傳 `115/07/03` 格式，用 `_roc_to_ymd()` 轉換。
7. **上櫃無 TWSE 資料** → 缺 key 是常態，一律 `.get()` + 降級，禁止 KeyError。
