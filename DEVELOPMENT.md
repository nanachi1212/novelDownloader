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
