"""番茄小說目錄與原始資料 Preview GUI。"""
import json
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QTextBrowser,
)

from app_paths import prepare_app_data
from fetcher import FetchError
from sites.fanqie import (
    AccessVerificationRequired, FanqieAdapter, FanqieError,
    chapter_cache_path, create_fetcher, open_reader_url, parse_book_id, save_raw_chapters,
    validate_raw_cache,
)

ACCESS_LABEL = {
    "public_candidate": "公開候選",
    "restricted": "付費／受限，略過",
    "unknown_locked": "鎖定狀態未明，略過",
    "unknown": "存取狀態不明，略過",
}


def load_cached_source_response(path, book_id, item_id):
    """Return the original API response text from a validated Preview envelope."""
    if not validate_raw_cache(path, book_id, item_id):
        raise FanqieError("Preview 快取無效，無法開啟原站回應。")
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = envelope.get("source_response_text")
    if not isinstance(raw, str):
        raise FanqieError("Preview 快取缺少原站回應文字。")
    return raw


class _FanqieWorker(QThread):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, mode, url, chapters=None, book_id="", cache_root=None, parent=None):
        super().__init__(parent)
        self.mode = mode
        self.url = url
        self.chapters = chapters or []
        self.book_id = book_id
        self.cache_root = Path(cache_root) if cache_root else None
        self.cancel_event = threading.Event()
        self.adapter = FanqieAdapter()

    def cancel(self):
        self.cancel_event.set()
        self.requestInterruption()

    def run(self):
        try:
            fetcher = create_fetcher()
            if self.mode == "directory":
                result = self.adapter.fetch_directory(self.url, fetcher)
                if not self.cancel_event.is_set():
                    self.completed.emit(result)
                return
            def announce(chapter):
                self.progress.emit(f"取得原始資料：{chapter.title}")

            saved = save_raw_chapters(
                self.adapter, self.book_id, self.chapters, self.cache_root,
                fetcher=fetcher, cancel_event=self.cancel_event, progress=announce,
            )
            self.completed.emit(saved)
        except (AccessVerificationRequired, FanqieError, FetchError, OSError, ValueError) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # Keep an unexpected response visible in the GUI.
            self.failed.emit(f"番茄預覽發生錯誤：{exc}")


class _RawPreview(QDialog):
    def __init__(self, title, raw, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"原始資料 Preview：{title}")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("以下是原站回應的原始 JSON，未正規化或修字。這不是原站字型 HTML 視覺預覽。"))
        viewer = QTextBrowser()
        viewer.setOpenLinks(False)
        viewer.setOpenExternalLinks(False)
        viewer.setPlainText(raw)
        layout.addWidget(viewer)
        close = QPushButton("關閉")
        close.clicked.connect(self.accept)
        layout.addWidget(close)


class FanqiePreviewDialog(QDialog):
    """可查看完整目錄、保存公開章節原始回應並開啟原站閱讀頁。"""

    def __init__(self, initial_url="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("番茄小說：預覽支援")
        self.resize(900, 680)
        self.cache_root = prepare_app_data() / "preview" / "fanqie"
        self.book_id = ""
        self.chapters = []
        self.worker = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("番茄小說：預覽支援"))
        url_row = QHBoxLayout()
        self.url_input = QLineEdit(initial_url)
        self.url_input.setObjectName("fanqieUrlInput")
        self.url_input.setPlaceholderText("貼上番茄小說書籍網址")
        url_row.addWidget(self.url_input)
        self.load_button = QPushButton("載入完整目錄")
        self.load_button.setObjectName("fanqieLoadDirectoryButton")
        self.load_button.clicked.connect(self.load_directory)
        url_row.addWidget(self.load_button)
        layout.addLayout(url_row)

        self.info = QLabel("目錄尚未載入；完整目錄會核對來源 ID 清單後顯示。")
        layout.addWidget(self.info)
        self.tree = QTreeWidget()
        self.tree.setObjectName("fanqieDirectoryTree")
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["卷名", "序", "章節", "存取狀態"])
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self.update_buttons)
        layout.addWidget(self.tree)

        controls = QHBoxLayout()
        self.save_selected_button = QPushButton("保存選取章節原始資料")
        self.save_selected_button.setObjectName("fanqieSaveSelectedButton")
        self.save_selected_button.clicked.connect(self.save_selected)
        controls.addWidget(self.save_selected_button)
        self.save_first_button = QPushButton("保存前三個公開候選章節")
        self.save_first_button.setObjectName("fanqieSaveFirstButton")
        self.save_first_button.clicked.connect(self.save_first_public)
        controls.addWidget(self.save_first_button)
        self.preview_button = QPushButton("開啟原始資料 Preview")
        self.preview_button.setObjectName("fanqieOpenPreviewButton")
        self.preview_button.clicked.connect(self.open_preview)
        controls.addWidget(self.preview_button)
        self.original_button = QPushButton("開啟原站閱讀器")
        self.original_button.setObjectName("fanqieOpenReaderButton")
        self.original_button.clicked.connect(self.open_original)
        controls.addWidget(self.original_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("fanqieCancelButton")
        self.cancel_button.clicked.connect(self.cancel_work)
        controls.addWidget(self.cancel_button)
        layout.addLayout(controls)

        self.status = QLabel("原始資料獨立保存在使用者資料目錄的 preview/fanqie 下，不會進入一般正文快取或 TXT／EPUB 匯出。")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.warning = QLabel("原始字元可能使用番茄自訂字型映射；視覺預覽不代表文字已還原，複製／搜尋可能不正確。")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)
        self.update_buttons()

    def load_directory(self):
        try:
            parse_book_id(self.url_input.text())
        except FanqieError as exc:
            QMessageBox.warning(self, "番茄網址錯誤", str(exc))
            return
        self._start(_FanqieWorker("directory", self.url_input.text().strip(), parent=self))

    def save_selected(self):
        chapter = self._selected_chapter()
        if chapter:
            self._save([chapter])

    def save_first_public(self):
        chapters = [ch for ch in self.chapters if ch.access == "public_candidate"][:3]
        if not chapters:
            QMessageBox.information(self, "沒有公開候選章節", "目錄中沒有存取旗標完整且明確公開的章節。")
            return
        self._save(chapters)

    def _save(self, chapters):
        if not self.book_id:
            return
        worker = _FanqieWorker("save", self.url_input.text().strip(), chapters,
                               self.book_id, self.cache_root, self)
        self._start(worker)

    def _start(self, worker):
        if self.worker and self.worker.isRunning():
            return
        self.worker = worker
        self.load_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.save_selected_button.setEnabled(False)
        self.save_first_button.setEnabled(False)
        self.status.setText("正在使用既有 Fetcher 與共用節流／重試流程。")
        worker.progress.connect(self.status.setText)
        worker.failed.connect(self.on_failed)
        worker.completed.connect(self.on_completed)
        worker.finished.connect(self.on_finished)
        worker.start()

    def cancel_work(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.status.setText("已要求取消；目前網路請求結束後停止。")

    def on_failed(self, message):
        self.status.setText(message)
        if "人機驗證" in message or "登入" in message or "購買" in message:
            self.status.setText(message + " 未下載正文。請使用原站正常閱讀流程。")
        self.update_buttons()

    def on_completed(self, result):
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
            self.book_id, self.chapters = result
            self._populate_directory()
            public_count = sum(ch.access == "public_candidate" for ch in self.chapters)
            self.info.setText(f"書籍 ID：{self.book_id}｜取得並核對 {len(self.chapters)} 章，{public_count} 章為公開候選。")
            for row, chapter in enumerate(self.chapters):
                if chapter.access == "public_candidate":
                    self.tree.setCurrentItem(self.tree.topLevelItem(row))
                    break
            self.status.setText("目錄完整性、順序與唯一 itemId 核對通過。")
            return
        count = 0
        for item_id, _path, result_kind in result:
            count += 1
            self._mark_cached(item_id)
            self.status.setText("已保存原始資料；" if result_kind == "saved" else "原始資料已存在，續接略過；")
        if count:
            self.status.setText(f"本次完成 {count} 章原始資料保存／續接。資料位於：{self.cache_root / self.book_id}")
        elif self.worker and self.worker.cancel_event.is_set():
            self.status.setText("已取消；已完成的 itemId 原始資料保留，下次可續接。")
        self.update_buttons()

    def on_finished(self):
        self.load_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if self.worker:
            self.worker.deleteLater()
            self.worker = None
        self.update_buttons()

    def _populate_directory(self):
        self.tree.clear()
        for chapter in self.chapters:
            item = QTreeWidgetItem([
                chapter.volume_name, str(chapter.order), chapter.title,
                ACCESS_LABEL.get(chapter.access, chapter.access),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, chapter.item_id)
            self.tree.addTopLevelItem(item)
            cached_path = chapter_cache_path(self.cache_root, self.book_id, chapter.item_id)
            if cached_path.is_file():
                if validate_raw_cache(cached_path, self.book_id, chapter.item_id):
                    self._mark_cached(chapter.item_id)
                else:
                    item.setText(3, ACCESS_LABEL.get(chapter.access, chapter.access) + "，快取無效")
                    item.setToolTip(3, "既有檔案保留未覆寫，請先人工檢查。")
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)
        self.update_buttons()

    def _selected_chapter(self):
        items = self.tree.selectedItems()
        if not items:
            return None
        item_id = items[0].data(0, Qt.ItemDataRole.UserRole)
        return next((chapter for chapter in self.chapters if chapter.item_id == item_id), None)

    def _selected_path(self):
        chapter = self._selected_chapter()
        if not chapter or not self.book_id:
            return None
        return chapter_cache_path(self.cache_root, self.book_id, chapter.item_id)

    def update_buttons(self):
        chapter = self._selected_chapter()
        path = self._selected_path()
        cache_exists = bool(path and path.is_file())
        can_read = bool(path and path.is_file() and self.book_id and chapter
                        and validate_raw_cache(path, self.book_id, chapter.item_id))
        self.save_selected_button.setEnabled(bool(chapter and chapter.access == "public_candidate" and not cache_exists and not (self.worker and self.worker.isRunning())))
        self.preview_button.setEnabled(can_read)
        self.original_button.setEnabled(chapter is not None)
        self.save_first_button.setEnabled(bool(self.chapters) and not (self.worker and self.worker.isRunning()))

    def _mark_cached(self, item_id):
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            if item.data(0, Qt.ItemDataRole.UserRole) == item_id:
                item.setText(3, ACCESS_LABEL["public_candidate"] + "，已保存")
                break

    def open_preview(self):
        path = self._selected_path()
        chapter = self._selected_chapter()
        if not path or not path.is_file() or not chapter:
            return
        try:
            raw = load_cached_source_response(path, self.book_id, chapter.item_id)
        except (OSError, FanqieError) as exc:
            QMessageBox.warning(self, "讀取原始資料失敗", str(exc))
            return
        _RawPreview(chapter.title, raw, self).exec()

    def open_original(self):
        chapter = self._selected_chapter()
        if chapter:
            QDesktopServices.openUrl(QUrl(open_reader_url(chapter.item_id)))

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.cancel_work()
            event.ignore()
            return
        super().closeEvent(event)
