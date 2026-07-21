# novel-downloader 小說下載器

輸入小說目錄頁(或簡介頁)網址,自動下載全書、過濾廣告、合併成單一 TXT。

## 用法

### GUI 版(推薦)

直接雙擊 `E:\AI gravity project\novelDownloader.exe`:

1. **貼上目錄頁網址**,可選填書名覆寫、起始章/結束章 → 按「**加入隊列**」
2. 重複步驟 1 可排多本書(**下載隊列**,依序執行;下載中也能繼續加)
3. 選儲存位置、調整延遲(預設 2.0 秒,被擋就調大)
4. 按「**開始下載**」;「停止」會在章節邊界安全中止,重新開始會從快取續傳
5. 失敗或停止的任務可按「失敗/停止的重設為等待」重跑

快取位置:EXE 旁邊的 `cache\` 資料夾(按 站名-書號 分資料夾,重抓同一本書不會重新下載已完成的章節)。

### 命令列版(進階)

```
cd "E:\AI gravity project\novel-downloader"
python novel_dl.py https://www.69shuba.com/book/67964.htm
python novel_dl.py <網址> --start 100 --end 200   # 只下載第 100~200 章
```

參數:`--out 輸出資料夾`、`--delay 秒`、`--start N`、`--end N`、`--limit N`、`--title 書名覆寫`

選項:

| 參數 | 說明 |
|------|------|
| `--out 路徑` | 輸出 TXT 位置(預設 `E:\AI gravity project\{書名}.txt`) |
| `--delay 秒` | 章節間延遲(預設 0.5,被擋的話調大) |
| `--limit N` | 只下載前 N 章(測試用) |

- **斷點續傳**:每章存在 `cache\`,中斷後重跑同一指令會跳過已下載的章節。想強制重抓就刪掉 `cache\{站名-書號}\`。
- 輸出為 UTF-8 純文字,保留原文簡體。

## 廣告過濾方式

1. **DOM 結構過濾**(主要):章節頁廣告是獨立元素(`div.contentadv`、`div.bottom-ad` 等),連同 script/連結一併移除,不會誤刪正文
2. **文字規則**(安全網):逐行過濾含「69书吧」、網址、「本章未完点击下一页」、「(本章完)」等行

## 支援網站

- 69shuba.com(69书吧)
- sunzhinan.com(醋溜儿文学) — 支持多頁章節自動縫接

### 新增其他網站

1. 在 `sites\` 加一個檔案,繼承 `SiteAdapter`(見 `sites\base.py`),實作:
   - `domains`:網域清單
   - `encoding`:頁面編碼
   - `catalog_url()` / `book_id()` / `parse_catalog()` / `parse_chapter()`
   - 選配 `meta_url()` / `parse_meta()`(書名作者在別頁時)
   - 選配 `next_page_url()`(一章拆成多頁的網站:回傳同章下一頁網址,主程式會自動逐頁抓完串接,並去除頁面交界重複的段落;注意要區分「同章下一頁」和「下一章」,通常看網址是 `456_2.html` 頁碼字尾還是章節 id 變了)
2. 在 `sites\__init__.py` 的 `ADAPTERS` 加上新 class

## 相依套件

```
pip install curl_cffi beautifulsoup4 lxml
```

`curl_cffi` 負責模擬真實瀏覽器的 TLS 指紋以通過 Cloudflare。注意:69shuba 的章節頁必須由同一個 session 先訪過目錄頁才抓得到(程式已自動處理)。
