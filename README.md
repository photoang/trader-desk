# 自營交易台

台股自營交易員的每日看盤入口，用 GitHub Pages 免費架設，GitHub Actions 每天自動抓資料。

- 網址：`https://<你的帳號>.github.io/trader-desk/`（原網站有綁自訂網域時，會是 `https://<自訂網域>/trader-desk/`）
- 這是獨立的 repo，不會影響原本的 GitHub 網站。

## 內容

| 區塊 | 來源 | 更新時間（台北） |
|---|---|---|
| 國際行情：加權、台積電、美股四大指數、費半、TSM ADR、VIX、日韓港陸歐股、美債、美元、原油、黃金 | Yahoo Finance（yfinance） | 每次排程 |
| 現貨三大法人買賣超 | 證交所 BFI82U | 約 15:40 後 |
| 台指期三大法人淨未平倉與走勢圖 | 期交所 futContractsDate | 約 15:40 後 |
| Put/Call Ratio | 期交所 pcRatio | 約 15:40 後 |
| 融資融券餘額 | 證交所 MI_MARGN | 約 21:50 後 |
| 各國財經新聞頭條：台灣、美國、中國香港、日本、韓國、歐洲 | 各媒體 RSS、鉅亨網、Google 新聞 | 每次排程 |
| 市場時鐘、結算日、看盤流程、資料連結 | 網頁本身計算 | 即時 |

## 第一次設定（約 5 分鐘）

1. 在 GitHub 建立新 repo，名稱 `trader-desk`，設為 **Public**，不要勾選加 README。
2. 把這個資料夾的所有檔案推上去：
   ```bash
   cd trader-desk
   git init -b main
   git add .
   git commit -m "初始版本"
   git remote add origin https://github.com/<你的帳號>/trader-desk.git
   git push -u origin main
   ```
3. 到 repo 的 **Settings → Pages**，**Source** 選 **GitHub Actions**。
4. 到 **Settings → Actions → General**，最下面 **Workflow permissions** 選 **Read and write permissions**，按 Save。
5. 到 **Actions** 分頁，點左邊「更新資料並部署」，按 **Run workflow** 手動跑第一次。
6. 跑完（綠色勾勾）後打開網址。若看不到資料，點進該次執行的「抓取資料」步驟看紀錄，每個來源會顯示 `[ok]`、`[none]` 或 `[fail]`。

## 常見問題

- **排程時間不準**：GitHub 排程常延遲 5 到 30 分鐘，尖峰時段偶爾會跳過一次，下一次會補上。
- **某個來源失敗**：程式會沿用上一次成功的資料，網頁不會壞掉。持續失敗通常是官網改版，調整 `scripts/fetch.py` 對應函式即可。
- **排程被停用**：公開 repo 若 60 天沒有任何活動，GitHub 會停用排程。程式每次更新資料都會 commit，通常不會發生；若發生，到 Actions 分頁按 Enable 即可。
- **想增減新聞來源**：修改 `scripts/fetch.py` 的 `FEEDS` 清單。想增減行情標的：修改 `TICKERS`。
- **本機測試**：
  ```bash
  pip install -r requirements.txt
  python scripts/fetch.py
  cd site && python -m http.server 8000   # 打開 http://localhost:8000
  ```

## 檔案結構

```
site/index.html            網頁
site/data/latest.json      最新行情與籌碼（排程產生）
site/data/series.json      每日序列，保留 250 個交易日（排程產生）
site/data/news.json        新聞頭條（排程產生）
scripts/fetch.py           抓資料程式
.github/workflows/update.yml  排程與部署
```

資料僅供參考，不構成投資建議。新聞只存標題與連結，內容請到原媒體閱讀。
