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
- sto9.com
- fanqienovel.com（Web 可取得完整正文的章節直接下載；Web 只有預覽時，可選用本機 Tomato bridge）

番茄小說可在 GUI 或 CLI 指定章節範圍（例如 `--start 11 --end 20`），也支援長篇下載。原有「番茄小說：預覽支援」仍可保存目錄、原始資料與閱讀頁。

### 番茄小說：選用的本機完整正文橋接

Tomato bridge 是**選用功能**，預設仍使用原本的 Web reader：

- Web 能取得完整正文的章節，直接下載，不會啟動 bridge。
- 只有 Web 僅提供預覽、或目錄未標示為公開的章節，才會交給你指定的 [TomatoNovelDownloader](https://github.com/zhongbai2333/Tomato-Novel-Downloader) EXE 取得完整正文；目錄未標示公開的章節會直接交給它，不會先嘗試 Web。同一次下載可以混合 Web 與 bridge 章節，輸出的章節順序不變。
- 取得的正文會寫入 novelDownloader 原本的章節快取，之後的 TXT／EPUB 輸出與 cache／續傳都與其他章節相同。已在快取內的章節重跑時不會再啟動 bridge。
- **第三方 EXE 不包含在 novelDownloader 的 release 內**，也不會由本程式下載或散布；請自行下載相容版本（已用 `TomatoNovelDownloader-Win64-v2.4.15.exe` 驗證）。
- 程式在本機 `127.0.0.1` 啟動該 EXE 的 `--server`（資料放在使用者資料夾的 `fanqie-bridge`）。如果預設埠 18423 已有你自己啟動的實例，本程式會改用另一個埠，不會使用也不會結束你的實例；取消下載時只會取消對應的 Tomato 任務，該程序會保留到關閉本程式時才結束；結束時只會結束自己啟動的那一個。
- 這只是「完整正文 provider」：本程式不繞過付費、SVIP、登入或平台權限，能否取得完整正文取決於網站當下的回應與你指定的工具。

#### 番茄小說完整正文下載

1. 下載 novelDownloader。
2. 若只下載 Web 可直接取得的章節，不需額外設定。
3. 若要下載 Web 僅提供預覽的章節：
   - 自行下載 TomatoNovelDownloader。
   - 在 novelDownloader GUI 按「番茄小說：完整正文橋接」，選擇該 EXE。
4. 正常貼入 fanqienovel.com 書籍網址。
5. 選擇章節範圍與 TXT／EPUB。
6. 開始下載。

番茄小說正文會自動解碼目前觀察到的 PUA 字元映射。若網站未來更換 PUA mapping，或不同書籍使用不同映射模式，程式會重新判定；無法可靠解碼時會停止該章下載並回報錯誤，不會把亂碼當成正常正文輸出。

其他網站會使用通用 adapter 嘗試解析。外部 `.py` adapter 能執行任意程式碼，因此匯入或下載後預設停用；檢查內容後才手動啟用。

## 快取與設定

打包版與直接執行原始碼，都把可變資料存入使用者資料目錄：

| 平台 | 預設目錄 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\novelDownloader`；環境變數無效時查詢 Windows 的 Local AppData |
| macOS | `~/Library/Application Support/novelDownloader` |
| Linux／其他 Unix | `$XDG_DATA_HOME/novelDownloader`，未設定或不是絕對路徑時使用 `~/.local/share/novelDownloader` |

- `cache/`：章節快取
- `queue.json`：下載隊列
- `preferences.json`：使用者偏好
- `site_settings.json`：網站延遲、並行、User-Agent、Referer 設定
- `history.json`：下載歷史
- `user_adapters/`：使用者自訂 adapter
- `filter_rules*.txt`：可編輯過濾規則（打包內建規則仍可唯讀載入）
- `novelDownloader.log` 與輪替檔：日誌

小說輸出仍依使用者選擇的下載位置；上述資料不再預設寫入 EXE 旁、PyInstaller 解壓目錄或 repository。

首次使用新目錄時，程式會從 EXE 旁（source mode 為原始碼目錄）複製既有資料，核對 SHA-256，**不刪除或改寫原件**。請先關閉舊版。新目錄已有同名檔案時保留新版，不自動合併 JSON；cache 補上缺少的章節，adapter 連同 `.py.disabled` 停用標記一起處理，既有 adapter 的啟用狀態優先。衝突會在 GUI 日誌／CLI 顯示，明細記錄在資料目錄的 `.migration-v1.json`。

複製失敗、資料損壞或無法寫入時會停止初始化並顯示路徑與原因；排除權限／空間問題後可重試。程式不退回安裝目錄寫入，也不把遷移失敗當作空白設定。遷移完成後不再匯入，避免清除 cache 或刪除 adapter 後又從舊目錄恢復。大型 cache 首次複製需要額外空間與時間，原件保留供復原；舊版此後的修改不會自動同步。

同一帳號的多份安裝與 source mode 預設共用此目錄。只自動遷移第一個來源；若切換到其他舊安裝，會提示其原件仍保留，不盲目混合多份隊列／設定。需要手動合併時，先關閉所有版本並備份新舊資料，隊列可使用原有匯入／匯出功能。不要刪除遷移紀錄來強迫重匯，否則可能恢復刻意刪除的舊資料。

開發或測試可把 `NOVELDOWNLOADER_DATA_DIR` 設為**絕對路徑**，選用獨立資料目錄。明確指定目錄時不自動匯入 EXE／原始碼旁的舊資料；frozen 模式仍拒絕將它設在安裝／bundle 內。pytest 會在收集測試前隔離此設定，避免讀取真實 user adapters 或寫入真實設定。

## 開發

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest
```

打包 Windows GUI：

```powershell
python -m PyInstaller gui_launcher.py --onefile --windowed --noconfirm --name novelDownloader-vX.Y.Z --add-data "sites;sites" --hidden-import curl_cffi
```
