"""GUI 啟動入口(供 PyInstaller 打包用)。"""
import os
import sys
import logging
from pathlib import Path

# PyInstaller onefile 的模組在 _MEIPASS，使用者資料則應放在 EXE 旁。
if getattr(sys, "frozen", False):
    app_dir = Path(sys.executable).parent
    bundle_dir = Path(getattr(sys, "_MEIPASS", app_dir))
else:
    app_dir = Path(__file__).parent
    bundle_dir = app_dir
sys.path.insert(0, str(bundle_dir))

# PyInstaller 6 may place native libraries in ``_internal``.  Explicitly
# register both locations so QtWidgets.pyd can find Qt6Widgets.dll on systems
# with restrictive DLL search settings.
if getattr(sys, "frozen", False):
    _dll_handles = []
    dll_dirs = (
        bundle_dir,
        bundle_dir / "_internal",
        bundle_dir / "PyQt6" / "Qt6" / "bin",
        bundle_dir / "_internal" / "PyQt6" / "Qt6" / "bin",
    )
    for dll_dir in dll_dirs:
        if dll_dir.is_dir():
            _dll_handles.append(os.add_dll_directory(str(dll_dir)))
            os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")

from PyQt6.QtWidgets import QApplication, QMessageBox
from app_paths import ApplicationDataError, migration_notice, prepare_app_data
from app_logging import configure_logging


def main():
    app = QApplication(sys.argv)
    try:
        prepare_app_data()
        configure_logging()
        # sites loads user adapters on import, so migration must finish first.
        from main_window import NovelDownloaderUI

        window = NovelDownloaderUI()
        notice = migration_notice()
        if notice:
            window.log.append(notice)
            logging.getLogger(__name__).warning(notice)
    except (ApplicationDataError, OSError) as exc:
        QMessageBox.critical(None, "應用程式資料無法載入", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
