# novelDownloader 小說下載器｜DEVELOPMENT

本文件是 `E:\\Codex project\\novelDownloader` 的維護文件。完整功能清單與版本資訊請先查看同一資料夾的 `README.md`。

## 建議流程

1. 建立獨立開發環境，不直接污染系統全域依賴。
2. 先執行現有測試或最小啟動。
3. 小幅修改並保留 diff。
4. 重新執行測試與手動 smoke test。
5. 只提交原始碼與必要設定，不提交快取、模型、密鑰或個人資料。

## 本專案環境

Python；PyQt6；curl_cffi；BeautifulSoup；PyInstaller

## 啟動／驗證

`python -m pip install --require-hashes -r requirements-dev.lock`；`python gui_launcher.py` 或 `python novel_dl.py`

修改 `requirements*.txt` 後，以 UTF-8 模式重新產生 lock file：

`$env:PYTHONUTF8='1'; python -m piptools compile --strip-extras --allow-unsafe --generate-hashes -o requirements.lock requirements.txt; python -m piptools compile --strip-extras --allow-unsafe --generate-hashes -o requirements-dev.lock requirements-dev.txt`

若實際版本與 README 不同，以鎖定檔、build 設定與當前錯誤訊息為準。

## 資料路徑與打包回歸驗證

執行 `python -m pytest -q`。測試在收集模組前使用獨立資料目錄；不得移除隔離設定，否則 `sites` 匯入時可能載入真實使用者的 adapter。

Windows 上分別驗證現有兩份 spec（不用修改 spec 或加入測試用 runtime hook）：

```powershell
python -m PyInstaller --noconfirm --distpath dist/onedir --workpath build/onedir novelDownloader.spec
python -m PyInstaller --noconfirm --distpath dist/onefile --workpath build/onefile novelDownloader-v1.6.0.spec
python tools/smoke_frozen.py dist/onedir/novelDownloader/novelDownloader.exe --work-dir build/smoke-onedir
python tools/smoke_frozen.py dist/onefile/novelDownloader-v1.6.0.exe --work-dir build/smoke-onefile
```

每次 smoke test 的 `--work-dir` 必須是新目錄；腳本不覆蓋既有測試資料。它複製打包產物，建立合成的舊版狀態與測試 adapter，以 offscreen Qt 啟動實際 EXE 兩次，檢查遷移、讀寫、重啟保存、停用標記、清除後不復活，以及安裝目錄所有檔案的 SHA-256 是否未變。只在子程序指定測試用 `LOCALAPPDATA`，不更動真實使用者目錄、不下載小說、不修改 ACL。

結果寫入各工作目錄的 `first.json`、`second.json` 與 `summary.json`。這是 Windows 自動化啟動／資料驗證，不能代替 Program Files 限制權限、中文使用者名稱、Windows 重新導向的 AppData、互動 GUI 與真實升級資料的人工驗證。Linux 模擬 frozen／Windows 路徑的 pytest 結果也不能視為 Windows EXE 實機驗證。
