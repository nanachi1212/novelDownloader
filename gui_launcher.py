"""GUI 啟動入口(供 PyInstaller 打包用)。"""
import sys
from pathlib import Path

# PyInstaller onefile 下 sys.argv[0] 是臨時檔,需要調整工作目錄
if getattr(sys, "frozen", False):
    app_dir = Path(sys.executable).parent
else:
    app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))

from PyQt6.QtWidgets import QApplication
from main_window import NovelDownloaderUI

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NovelDownloaderUI()
    window.show()
    sys.exit(app.exec())
