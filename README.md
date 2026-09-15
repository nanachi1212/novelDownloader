# novelDownloader 小說下載器

輸入小說目錄頁或簡介頁網址，自動下載章節、過濾廣告，輸出 TXT 或 EPUB。

## GUI

最新版 Windows 執行檔請到 GitHub Releases 下載：

- https://github.com/nanachi1212/novelDownloader/releases

基本流程：

1. 貼上小說網址，可選填書名、起始章與結束章。
2. 按「加入隊列」，可一次排多本書。
3. 選儲存位置、延遲、同時下載數、章節並行、timeout、重試次數與輸出格式。
4. 按「開始下載」。停止會在章節邊界中止，重跑會沿用快取續傳。

隊列支援搜尋、篩選、拖曳排序、匯入/匯出 JSON、單本開始/停止、移除已完成、右鍵複製網址，以及每本書折疊式進度。

## CLI

```powershell
python novel_dl.py https://www.69shuba.com/book/67964.htm
python novel_dl.py <網址> --start 100 --end 200
```

常用參數：

- `--out 路徑`：輸出資料夾
- `--delay 秒`：章節間延遲
- `--chapter-workers N`：單本小說同時下載章節數（預設 3，最高 8）
- `--timeout 秒`：單次連線等待上限
- `--start N` / `--end N`：下載章節範圍
- `--limit N`：只下載前 N 章
- `--title 書名`：覆寫書名

## 支援網站

內建專用 adapter：

- 69shuba.com / 69shuba.tw
- twkan.com（可自動載入「展開全部」完整目錄）
- czbooks.net
- xbanxia.cc
- sunzhinan.com
- 52shuku.net
- novel543.com
- 8book.com
- novels.com.tw
- twp.zhys.tw

其他網站會使用通用 adapter 嘗試解析。外部 `.py` adapter 能執行任意程式碼，因此匯入或下載後預設停用；檢查內容後才手動啟用。

## 快取與設定

程式會在執行檔旁保存：

- `cache/`：章節快取
- `queue.json`：下載隊列
- `site_settings.json`：網站延遲、並行、User-Agent、Referer 設定
- `user_adapters/`：使用者自訂 adapter

這些都是本機執行資料，不應提交到 repo。

## 開發

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

打包 Windows GUI：

```powershell
python -m PyInstaller gui_launcher.py --onefile --windowed --noconfirm --name novelDownloader-vX.Y.Z --add-data "sites;sites" --hidden-import curl_cffi
```
