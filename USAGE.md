# novelDownloader 小說下載器｜USAGE

本文件是 `E:\\Codex project\\novelDownloader` 的維護文件。完整功能清單與版本資訊請先查看同一資料夾的 `README.md`。

## 啟動方式

執行 `python -m pip install -r requirements-dev.txt`，再執行 `python gui_launcher.py` 或 `python novel_dl.py <網址>`。

下載太慢時可提高「章節並行」（CLI：`--chapter-workers`）；遇到 HTTP 429 或 Cloudflare 時應降回 1，並提高章節延遲。

## 日常使用

先從 README 的「使用方式」開始；輸入資料前確認輸出資料夾與備份位置。遇到錯誤請保留完整錯誤訊息，不要反覆刪除資料夾。

## 停止與備份

關閉程式前先等待工作完成；修改前複製設定檔、資料庫或快取。
