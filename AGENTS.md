# novelDownloader Repository Instructions

本檔只補充 novelDownloader 專案特有規則。
一般開發流程、Git/GitHub、安全、驗證與回報方式遵循使用者層 Global `AGENTS.md`。

## Product Identity

- novelDownloader 是以 Windows GUI 為主要使用方式、CLI 為輔的小說下載工具。
- 核心用途是從小說目錄頁或簡介頁擷取章節，經過站點 adapter 與過濾規則後輸出 TXT 或 EPUB。
- 優先維持既有 GUI、CLI、queue、cache、adapter 與輸出格式的相容性，不為單一網站建立破壞整體架構的特殊旁路。

## Adapter Architecture

- 站點差異應優先封裝在既有 site adapter / parser 邊界內。
- 通用 downloader、queue、cache、filter 與輸出邏輯不要複製成網站專屬版本。
- 新增或修改 adapter 時，要處理：
  - 目錄解析
  - 章節網址
  - 標題與正文擷取
  - 編碼與反爬失敗
  - 空頁、重導向與暫時性錯誤
  - 必要的 rate limiting / retry 語意
- 外部使用者 adapter 能執行任意 Python 程式碼；維持既有預設停用與明確啟用模型，不要默默提高其信任等級。

## User Data and Migration Safety

可變資料預設位於使用者資料目錄，而不是 repository、EXE 旁或 PyInstaller 解壓目錄。

涉及以下內容時，優先保護使用者既有資料：

- `cache/`
- `queue.json`
- `preferences.json`
- `site_settings.json`
- `history.json`
- `user_adapters/`
- filter rules
- migration metadata

修改資料路徑或 migration 時：

- 不刪除或覆寫舊資料作為遷移手段。
- 新舊位置衝突時應保留可恢復性並提供明確結果。
- migration 應可重試、可判斷是否已完成，且不能因 cache 被清除就自動重新匯入舊資料。
- 測試必須隔離真實 user data，不讀寫使用者實際設定、cache 或 adapter。
- 不要把 migration 失敗當成空白設定並繼續啟動。

## Download Semantics

- 停止下載應盡量維持在安全章節邊界，不產生看似完成但實際截斷的輸出。
- queue、resume、cache 與重試邏輯修改時，要檢查單本與多本並行的狀態一致性。
- 不要因單一章節或單一站點失敗而破壞其他 queue item 的狀態。
- 修改並行、timeout、retry 或 rate limit 時，注意不要讓預設值造成對站點不必要的高頻請求。

## Output Integrity

- TXT / EPUB 輸出必須保留章節順序。
- 過濾廣告或清洗內容時，優先避免誤刪正文。
- cache 命中不得繞過必要的完整性檢查。
- 輸出或 cache 寫入失敗時，不把 partial file 標示為正常完成。

## Windows and Packaging

- Windows GUI 是主要交付面；優先使用 PowerShell 相容指令與 Windows 路徑語意。
- PyInstaller、bundle path、user data path 與 source mode 的路徑行為不可混用。
- 只有涉及 packaging、launcher、resource bundling 或 release 時才需要執行 PyInstaller 相關驗證；一般 parser / queue / filter 修改不需無條件重新打包完整 EXE。

## Context Routing

不要每次工作都完整掃描所有站點與文件。

- 修改特定網站：先讀該 adapter、對應 fixtures/tests 與共享 parser contract。
- 修改 queue / resume / cache：讀相關 state persistence 與 concurrency 路徑。
- 修改 user data path / migration：讀 app path、migration、startup 與其測試。
- 修改 packaging：讀 launcher、PyInstaller 設定與 packaging 文件。
- 修改 filter：讀 filter pipeline 與代表性內容測試。

## Validation

驗證與變更範圍相稱。

- adapter/parser：跑對應站點與 parser targeted tests。
- queue/cache/resume：跑狀態、並行與 persistence 相關 tests。
- migration/app paths：跑 user-data isolation、migration 與 frozen/source mode 相關 tests。
- filter：跑代表性正文與誤刪邊界測試。
- packaging：只有相關修改才跑 PyInstaller / frozen smoke test。
- 只有跨多個核心模組或 release candidate 才需要完整 pytest / packaging regression。
