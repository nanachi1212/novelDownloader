"""GUI 啟動入口(供 PyInstaller 打包用)。"""
import os
import sys
from pathlib import Path

# PyInstaller onefile 下 sys.argv[0] 是臨時檔,需要調整工作目錄
if getattr(sys, "frozen", False):
    app_dir = Path(sys.executable).parent
else:
    app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))

# PyInstaller 6 may place native libraries in ``_internal``.  Explicitly
# register both locations so QtWidgets.pyd can find Qt6Widgets.dll on systems
# with restrictive DLL search settings.
if getattr(sys, "frozen", False):
    for dll_dir in (app_dir, app_dir / "_internal"):
        if dll_dir.is_dir():
            os.add_dll_directory(str(dll_dir))

from PyQt6.QtWidgets import QApplication
from main_window import NovelDownloaderUI
from app_logging import configure_logging

if __name__ == "__main__":
    configure_logging()
    app = QApplication(sys.argv)
    window = NovelDownloaderUI()
    window.show()
    sys.exit(app.exec())
