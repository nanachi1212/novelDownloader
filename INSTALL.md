# novelDownloader 小說下載器｜INSTALL

本文件是 `E:\\Codex project\\novelDownloader` 的維護文件。完整功能清單與版本資訊請先查看同一資料夾的 `README.md`。

## 需要準備

Python 3.12；直接套件記錄於 `requirements.txt` 與 `requirements-dev.txt`，可重現安裝使用含傳遞套件與雜湊的 lock file。

## 安裝

1. 以檔案總管進入專案資料夾。
2. 執行 `python -m pip install --require-hashes -r requirements-dev.lock`。
3. 安裝完成後先執行最小啟動測試。

## 成功判定

程式能啟動、主要畫面能開啟，且沒有立即的模組／路徑錯誤。
