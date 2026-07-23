"""PyQt6 圖形界面:下載隊列、章節範圍、儲存位置、書名編輯、進度與日誌。"""
import sys
import threading
import json
import time
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QProgressBar,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QMenu,
    QFileDialog,
    QMessageBox,
    QSpinBox,
    QDialog,
    QAbstractItemView,
    QSystemTrayIcon,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QDesktopServices
from PyQt6.QtCore import QUrl

from downloader_task import Cancelled, download_novel, cache_root
from fetcher import Fetcher
from adapter_tools import (
    install_adapter_file,
    list_user_adapter_files,
    user_adapter_dir,
    write_generated_adapter,
    toggle_adapter_enabled,
    adapter_is_enabled,
)
from textfilter import ensure_rules_file, rules_path
from PyQt6.QtWidgets import QComboBox
from sites import ADAPTERS, USER_ADAPTER_ERRORS, get_adapter, reload_adapters

APP_VERSION = "1.4.0"

STATUS_LABEL = {
    "pending": "⏳ 等待",
    "running": "▶ 下載中",
    "done": "✓ 完成",
    "failed": "✗ 失敗",
    "stopped": "⏸ 已停止(可續傳)",
    "removing": "🗑 刪除中",
    "removed": "🗑 已刪除",
}


def site_key_for_url(url: str) -> str:
    """Queue throttling key. Registered adapters use their primary domain."""
    try:
        adapter = get_adapter(url)
        if getattr(adapter, "domains", None):
            return adapter.domains[0]
    except Exception:
        pass
    host = urlparse(url).netloc.lower().split(":")[0]
    return host or "unknown"


def classify_download_error(error) -> str:
    text = str(error)
    lowered = text.lower()
    if "403" in lowered or "access denied" in lowered:
        return f"HTTP 403：網站拒絕請求，可能是 IP、Cookie 或 Cloudflare 封鎖。{text}"
    if "429" in lowered or "too many requests" in lowered:
        return f"HTTP 429：請求過於頻繁，請降低並行數或提高延遲。{text}"
    if "cloudflare" in lowered or "just a moment" in lowered:
        return f"Cloudflare 人機驗證：請先用瀏覽器通過驗證或更換網路。{text}"
    if "timeout" in lowered or "timed out" in lowered:
        return f"連線逾時：可提高延遲或稍後重試。{text}"
    return text


class QueueTreeWidget(QTreeWidget):
    order_changed = pyqtSignal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()


class QueueThread(QThread):
    """以可設定的工作數並行處理書籍；每本書內的章節仍依序下載。"""

    sig_log = pyqtSignal(str)
    sig_job_log = pyqtSignal(int, str)
    sig_progress = pyqtSignal(int, int, int)
    sig_stats = pyqtSignal(int, int, int)
    sig_job_status = pyqtSignal(int, str)
    sig_all_done = pyqtSignal()

    def __init__(self, jobs, output_dir, delay, max_workers=2, max_workers_per_site=6,
                 retries=5, output_format="txt", site_settings=None, filename_format="title",
                 site_cookies=None):
        super().__init__()
        self.jobs = jobs
        self.output_dir = output_dir
        self.delay = delay
        self.max_workers = max(1, int(max_workers))
        self.max_workers_per_site = max(1, int(max_workers_per_site))
        self.retries = max(1, int(retries))
        self.output_format = output_format
        self.site_settings = site_settings or {}
        self.filename_format = filename_format
        self.site_cookies = site_cookies or {}
        self._stop_event = threading.Event()
        self._jobs_lock = threading.Lock()
        for job in self.jobs:
            job.setdefault("stop_event", threading.Event())

    @property
    def stop_requested(self):
        return self._stop_event.is_set()

    def request_stop(self):
        self._stop_event.set()

    def request_job_stop(self, row):
        """停止指定工作；執行中的工作會在目前章節完成後停止。"""
        with self._jobs_lock:
            if row < 0 or row >= len(self.jobs):
                return False
            job = self.jobs[row]
            job.setdefault("stop_event", threading.Event()).set()
            if job["status"] == "pending":
                job["status"] = "stopped"
                stopped = True
            else:
                stopped = False
        if stopped:
            self.sig_job_status.emit(row, "stopped")
        return True

    def request_job_remove(self, row):
        """移除指定工作；執行中的工作先在章節邊界安全停止。"""
        with self._jobs_lock:
            if row < 0 or row >= len(self.jobs):
                return False
            job = self.jobs[row]
            job["remove_requested"] = True
            job.setdefault("stop_event", threading.Event()).set()
            if job["status"] == "pending":
                job["status"] = "removed"
                removed = True
            else:
                removed = False
        if removed:
            self.sig_job_status.emit(row, "removed")
        return True

    def request_job_start(self, row):
        """將指定工作重新放回等待隊列，或取消尚未生效的單獨停止。"""
        with self._jobs_lock:
            if row < 0 or row >= len(self.jobs):
                return False
            job = self.jobs[row]
            job.setdefault("stop_event", threading.Event()).clear()
            if job["status"] in ("stopped", "failed"):
                job["status"] = "pending"
                pending = True
            else:
                pending = job["status"] == "pending"
        if pending:
            self.sig_job_status.emit(row, "pending")
        return True

    def _job_cancel_requested(self, row):
        with self._jobs_lock:
            event = self.jobs[row].get("stop_event")
        return self.stop_requested or (event is not None and event.is_set())

    def _claim_pending_job(self):
        """原子地領取一筆可執行工作,並套用同站並行上限。"""
        with self._jobs_lock:
            running_by_site = {}
            for job in self.jobs:
                if job["status"] == "running":
                    site_key = job.get("site_key") or site_key_for_url(job["url"])
                    running_by_site[site_key] = running_by_site.get(site_key, 0) + 1
            for row, job in enumerate(self.jobs):
                if job["status"] == "pending":
                    site_key = job.get("site_key") or site_key_for_url(job["url"])
                    site_limit = int(self.site_settings.get(site_key, {}).get(
                        "max_workers", self.max_workers_per_site))
                    if running_by_site.get(site_key, 0) >= max(1, site_limit):
                        continue
                    job["status"] = "running"
                    job["site_key"] = site_key
                    return row, job
        return None

    def _has_running_job(self):
        with self._jobs_lock:
            return any(job["status"] == "running" for job in self.jobs)

    def _run_worker(self):
        while not self.stop_requested:
            claimed = self._claim_pending_job()
            if claimed is None:
                # 其他 worker 還在忙時稍候，讓執行中新增的任務也能被領取。
                if self._has_running_job():
                    self._stop_event.wait(0.1)
                    continue
                break

            row, job = claimed
            self.sig_job_status.emit(row, "running")
            prefix = job["title"] or f"隊列 {row + 1}"
            try:
                def cb(stage, current, total, msg):
                    if stage == "chapter":
                        self.sig_progress.emit(row, current, total)
                        self.sig_stats.emit(row, current, total)
                        if current == 1 or current == total or current % 10 == 0:
                            self.sig_job_log.emit(row, msg)
                            self.sig_log.emit(f"[{prefix}] {msg}")
                    else:
                        self.sig_job_log.emit(row, msg)
                        self.sig_log.emit(f"[{prefix}] {msg}")

                job_delay = float(self.site_settings.get(job.get("site_key", ""), {}).get(
                    "delay", self.delay))
                site_settings = self.site_settings.get(job.get("site_key", ""), {})
                request_headers = {}
                if site_settings.get("user_agent"):
                    request_headers["User-Agent"] = site_settings["user_agent"]
                if site_settings.get("referer"):
                    request_headers["Referer"] = site_settings["referer"]
                if self.site_cookies.get(job.get("site_key", "")):
                    request_headers["Cookie"] = self.site_cookies[job["site_key"]]
                download_novel(
                    job["url"], self.output_dir, job["title"], job_delay, cb,
                    start=job["start"], end=job["end"],
                    cancel_check=lambda: self._job_cancel_requested(row),
                    retries=self.retries, output_format=self.output_format,
                    filename_format=self.filename_format,
                    request_headers=request_headers,
                )
                job["status"] = "done"
                self.sig_job_status.emit(row, "done")
            except Cancelled:
                if job.get("remove_requested"):
                    job["status"] = "removed"
                    self.sig_job_status.emit(row, "removed")
                    self.sig_job_log.emit(row, "已停止並從隊列移除。")
                    self.sig_log.emit(f"[{prefix}] 已停止並從隊列移除。")
                else:
                    job["status"] = "stopped"
                    self.sig_job_status.emit(row, "stopped")
                    self.sig_job_log.emit(row, "已停止；重新開始會從快取續傳。")
                    self.sig_log.emit(f"[{prefix}] 已停止；重新開始會從快取續傳。")
            except Exception as e:
                if self._is_transient_error(e) and job.get("auto_retry", 0) < 2:
                    job["auto_retry"] = job.get("auto_retry", 0) + 1
                    job["status"] = "pending"
                    wait_seconds = 3 * job["auto_retry"]
                    self.sig_job_status.emit(row, "pending")
                    self.sig_job_log.emit(row, f"網路中斷，{wait_seconds} 秒後自動恢復（第 {job['auto_retry']}/2 次）")
                    self.sig_log.emit(f"[{prefix}] 網路中斷，將自動恢復。")
                    self._stop_event.wait(wait_seconds)
                    continue
                job["status"] = "failed"
                detail = classify_download_error(e)
                self.sig_job_status.emit(row, "failed")
                self.sig_job_log.emit(row, f"✗ 下載失敗：{detail}")
                self.sig_log.emit(f"[{prefix}] ✗ 下載失敗：{detail}")

    @staticmethod
    def _is_transient_error(error):
        text = str(error).lower()
        return any(word in text for word in ("timeout", "timed out", "connection", "temporarily", "503", "502", "504"))

    def run(self):
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            workers = [executor.submit(self._run_worker) for _ in range(self.max_workers)]
            for worker in workers:
                worker.result()
        self.sig_all_done.emit()


class NovelDownloaderUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"小說下載器 v{APP_VERSION}")
        self.setGeometry(100, 100, 860, 680)
        self.selected_dir = Path.home() / "Downloads"
        self.jobs = []  # [{url,title,start,end,status}]
        self.thread = None
        self.stats_started = 0.0
        self.chapter_progress = {}
        self.queue_file = (Path(sys.executable).parent if getattr(sys, "frozen", False)
                           else Path(__file__).parent) / "queue.json"
        self.site_settings_file = self.queue_file.parent / "site_settings.json"
        self.history_file = self.queue_file.parent / "history.json"
        self.site_settings = self.load_site_settings()
        self.site_cookies = {}
        self.tray = QSystemTrayIcon(self) if QSystemTrayIcon.isSystemTrayAvailable() else None
        if self.tray:
            self.tray.show()

        widget = QWidget()
        layout = QVBoxLayout()

        # --- 新增任務區（簡化） ---
        layout.addWidget(QLabel("小說 URL:"))
        url_layout = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("貼上小說目錄頁或簡介頁網址")
        url_layout.addWidget(self.url_input)
        paste_btn = QPushButton("貼上")
        paste_btn.setMaximumWidth(50)
        paste_btn.clicked.connect(self.paste_from_clipboard)
        url_layout.addWidget(paste_btn)
        layout.addLayout(url_layout)

        # --- 選項行（更緊湊） ---
        opt_layout = QHBoxLayout()
        opt_layout.addWidget(QLabel("書名:"))
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("可空")
        self.title_input.setMaximumWidth(150)
        opt_layout.addWidget(self.title_input)

        opt_layout.addWidget(QLabel("章節:"))
        opt_layout.addWidget(QLabel("第"))
        self.start_input = QLineEdit()
        self.start_input.setPlaceholderText("1")
        self.start_input.setMaximumWidth(50)
        opt_layout.addWidget(self.start_input)
        opt_layout.addWidget(QLabel("~"))
        self.end_input = QLineEdit()
        self.end_input.setPlaceholderText("末")
        self.end_input.setMaximumWidth(50)
        opt_layout.addWidget(self.end_input)
        opt_layout.addWidget(QLabel("章"))

        self.add_btn = QPushButton("➕ 加入隊列")
        self.add_btn.clicked.connect(self.add_job)
        opt_layout.addWidget(self.add_btn)
        opt_layout.addStretch()
        layout.addLayout(opt_layout)

        # --- 隊列 ---
        layout.addWidget(QLabel("下載隊列(可同時下載多本；每本書的章節仍依序執行):"))
        queue_tools = QHBoxLayout()
        queue_tools.addWidget(QLabel("搜尋:"))
        self.queue_search = QLineEdit()
        self.queue_search.setPlaceholderText("輸入書名、網站或網址篩選隊列")
        self.queue_search.textChanged.connect(self.filter_queue)
        queue_tools.addWidget(self.queue_search)
        self.export_queue_btn = QPushButton("匯出隊列")
        self.export_queue_btn.clicked.connect(self.export_queue)
        queue_tools.addWidget(self.export_queue_btn)
        self.import_queue_btn = QPushButton("匯入隊列")
        self.import_queue_btn.clicked.connect(self.import_queue)
        queue_tools.addWidget(self.import_queue_btn)
        layout.addLayout(queue_tools)
        self.queue_list = QueueTreeWidget()
        self.queue_list.setHeaderHidden(True)
        self.queue_list.setMaximumHeight(140)
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.queue_list.order_changed.connect(self.sync_queue_order)
        self.queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self.show_queue_menu)
        layout.addWidget(self.queue_list)
        self.load_queue()
        qbtn_layout = QHBoxLayout()
        self.remove_btn = QPushButton("移除選中")
        self.remove_btn.setToolTip("下載中也可移除；執行中的任務會先安全停止")
        self.remove_btn.clicked.connect(self.remove_selected)
        qbtn_layout.addWidget(self.remove_btn)
        self.retry_btn = QPushButton("失敗/停止的重設為等待")
        self.retry_btn.clicked.connect(self.reset_failed)
        qbtn_layout.addWidget(self.retry_btn)
        self.remove_done_btn = QPushButton("移除已完成")
        self.remove_done_btn.setToolTip("從隊列移除所有已完成下載的任務")
        self.remove_done_btn.clicked.connect(self.remove_completed)
        qbtn_layout.addWidget(self.remove_done_btn)
        self.start_selected_btn = QPushButton("單獨開始")
        self.start_selected_btn.setToolTip("將選中的失敗/停止任務重新放回等待隊列")
        self.start_selected_btn.clicked.connect(self.start_selected)
        qbtn_layout.addWidget(self.start_selected_btn)
        self.stop_selected_btn = QPushButton("單獨停止")
        self.stop_selected_btn.setToolTip("停止選中的任務；執行中會在目前章節完成後停止")
        self.stop_selected_btn.clicked.connect(self.stop_selected)
        qbtn_layout.addWidget(self.stop_selected_btn)
        self.batch_start_btn = QPushButton("批次開始")
        self.batch_start_btn.clicked.connect(self.start_selected)
        qbtn_layout.addWidget(self.batch_start_btn)
        self.batch_stop_btn = QPushButton("批次停止")
        self.batch_stop_btn.clicked.connect(self.stop_selected)
        qbtn_layout.addWidget(self.batch_stop_btn)
        qbtn_layout.addStretch()
        layout.addLayout(qbtn_layout)

        # --- 全域設定 ---
        dir_layout = QHBoxLayout()
        dir_layout.addWidget(QLabel("儲存位置:"))
        self.dir_label = QLineEdit()
        self.dir_label.setReadOnly(True)
        self.dir_label.setText(str(self.selected_dir))
        dir_layout.addWidget(self.dir_label)
        self.browse_btn = QPushButton("瀏覽...")
        self.browse_btn.clicked.connect(self.choose_directory)
        dir_layout.addWidget(self.browse_btn)
        dir_layout.addWidget(QLabel("章節延遲(秒):"))
        self.delay_input = QLineEdit()
        self.delay_input.setText("2.0")
        self.delay_input.setMaximumWidth(70)
        dir_layout.addWidget(self.delay_input)
        dir_layout.addWidget(QLabel("同時下載:"))
        self.concurrent_input = QSpinBox()
        self.concurrent_input.setRange(1, 10)
        self.concurrent_input.setValue(10)
        self.concurrent_input.setToolTip("整個隊列同時下載的小說本數（1～10）；數量過高可能被網站限制")
        self.concurrent_input.setMinimumWidth(80)
        dir_layout.addWidget(self.concurrent_input)
        dir_layout.addWidget(QLabel("同網站最多:"))
        self.site_concurrent_input = QSpinBox()
        self.site_concurrent_input.setRange(1, 6)
        self.site_concurrent_input.setValue(6)
        self.site_concurrent_input.setToolTip("同一網站同時下載的小說本數；預設 6 不限制原本速度，被限制時再降到 1～2")
        self.site_concurrent_input.setMinimumWidth(80)
        dir_layout.addWidget(self.site_concurrent_input)
        self.site_settings_btn = QPushButton("網站設定...")
        self.site_settings_btn.setToolTip("為特定網站覆寫延遲與同網站並行上限")
        self.site_settings_btn.clicked.connect(self.edit_site_settings)
        dir_layout.addWidget(self.site_settings_btn)
        self.cookies_btn = QPushButton("Cookie...")
        self.cookies_btn.setToolTip("匯入目前瀏覽器 Cookie；只保存在本次執行期間")
        self.cookies_btn.clicked.connect(self.edit_cookies)
        dir_layout.addWidget(self.cookies_btn)
        dir_layout.addWidget(QLabel("重試:"))
        self.retry_input = QSpinBox()
        self.retry_input.setRange(1, 10)
        self.retry_input.setValue(5)
        self.retry_input.setToolTip("每個網頁請求失敗時的重試次數")
        self.retry_input.setMinimumWidth(65)
        dir_layout.addWidget(self.retry_input)
        dir_layout.addWidget(QLabel("輸出:"))
        self.output_format_input = QComboBox()
        self.output_format_input.addItem("TXT", "txt")
        self.output_format_input.addItem("EPUB", "epub")
        self.output_format_input.setToolTip("TXT 或 EPUB 輸出格式")
        dir_layout.addWidget(self.output_format_input)
        dir_layout.addWidget(QLabel("檔名:"))
        self.filename_format_input = QComboBox()
        self.filename_format_input.addItem("書名", "title")
        self.filename_format_input.addItem("作者_書名", "author_title")
        self.filename_format_input.addItem("網站_書名", "site_title")
        dir_layout.addWidget(self.filename_format_input)
        self.rules_btn = QPushButton("過濾規則...")
        self.rules_btn.clicked.connect(self.edit_rules)
        dir_layout.addWidget(self.rules_btn)
        self.adapter_btn = QPushButton("Adapter 工具...")
        self.adapter_btn.clicked.connect(self.edit_adapters)
        dir_layout.addWidget(self.adapter_btn)
        self.history_btn = QPushButton("下載歷史")
        self.history_btn.clicked.connect(self.show_history)
        dir_layout.addWidget(self.history_btn)
        self.cache_btn = QPushButton("清理快取")
        self.cache_btn.clicked.connect(self.clear_cache)
        dir_layout.addWidget(self.cache_btn)
        layout.addLayout(dir_layout)

        # --- 進度與日誌 ---
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.stats_started = time.monotonic()
        self.chapter_progress = {}
        layout.addWidget(self.progress)
        self.progress_info = QLabel("速度：-- ｜ 預估剩餘：--")
        layout.addWidget(self.progress_info)
        log_font = QFont("Courier")
        log_font.setPointSize(9)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(log_font)
        layout.addWidget(self.log)

        # --- 控制 ---
        ctrl_layout = QHBoxLayout()
        self.start_btn = QPushButton("開始下載")
        self.start_btn.clicked.connect(self.start_queue)
        ctrl_layout.addWidget(self.start_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_queue)
        ctrl_layout.addWidget(self.stop_btn)
        layout.addLayout(ctrl_layout)

        widget.setLayout(layout)
        self.setCentralWidget(widget)

    # --- 隊列操作 ---
    @staticmethod
    def job_site_label(job):
        return job.get("site_key") or site_key_for_url(job["url"])

    def paste_from_clipboard(self):
        """從剪貼板貼上 URL"""
        clipboard = QApplication.clipboard()
        text = clipboard.text().strip()
        if text:
            self.url_input.setText(text)
        else:
            QMessageBox.warning(self, "提示", "剪貼板是空的")

    def job_text(self, job):
        rng = f"第{job['start'] or 1}~{job['end'] or '末'}章" if (job["start"] or job["end"]) else "全書"
        name = job["title"] or "(自動書名)"
        return f"{STATUS_LABEL[job['status']]} | {self.job_site_label(job)} | {name} | {rng} | {job['url']}"

    def job_item(self, row):
        item = self.queue_list.topLevelItem(row)
        return item if item else None

    def make_job_item(self, job):
        job.setdefault("id", uuid.uuid4().hex)
        item = QTreeWidgetItem([self.job_text(job)])
        item.setData(0, Qt.ItemDataRole.UserRole, job["id"])
        return item

    def sync_queue_order(self):
        if self.thread and self.thread.isRunning():
            return
        by_id = {job.get("id"): job for job in self.jobs}
        ordered = []
        for row in range(self.queue_list.topLevelItemCount()):
            item = self.queue_list.topLevelItem(row)
            job = by_id.get(item.data(0, Qt.ItemDataRole.UserRole))
            if job:
                ordered.append(job)
        if len(ordered) == len(self.jobs):
            self.jobs[:] = ordered
            self.save_queue()

    def add_job(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "輸入不完整", "請輸入目錄頁 URL")
            return
        try:
            start = int(self.start_input.text()) if self.start_input.text().strip() else None
            end = int(self.end_input.text()) if self.end_input.text().strip() else None
        except ValueError:
            QMessageBox.warning(self, "輸入錯誤", "起始/結束章必須是整數")
            return
        if start and end and start > end:
            QMessageBox.warning(self, "輸入錯誤", "起始章不能大於結束章")
            return
        job = {"url": url, "title": self.title_input.text().strip(),
               "start": start, "end": end, "status": "pending",
               "site_key": site_key_for_url(url)}
        job["id"] = uuid.uuid4().hex
        self.jobs.append(job)
        self.queue_list.addTopLevelItem(self.make_job_item(job))
        self.save_queue()
        self.url_input.clear()
        self.title_input.clear()
        self.start_input.clear()
        self.end_input.clear()

    def remove_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        if self.thread and self.thread.isRunning():
            for row in rows:
                job = self.jobs[row]
                if job["status"] == "running":
                    self.thread.request_job_remove(row)
                    self.refresh_row(row, "removing")
                elif job["status"] not in ("removed",):
                    job["remove_requested"] = True
                    job["status"] = "removed"
                    self.refresh_row(row, "removed")
            self.log.append(f"已要求移除 {len(rows)} 個隊列項目。")
            return
        for row in reversed(rows):
            self.jobs.pop(row)
            self.queue_list.takeTopLevelItem(row)
        self.save_queue()

    def selected_row(self):
        item = self.queue_list.currentItem()
        if item and item.parent():
            item = item.parent()
        return self.queue_list.indexOfTopLevelItem(item) if item else -1

    def selected_rows(self):
        rows = set()
        for item in self.queue_list.selectedItems():
            if item.parent():
                item = item.parent()
            row = self.queue_list.indexOfTopLevelItem(item)
            if row >= 0:
                rows.add(row)
        if not rows:
            row = self.selected_row()
            if row >= 0:
                rows.add(row)
        return sorted(rows)

    def start_selected(self):
        rows = self.selected_rows()
        if not rows:
            QMessageBox.information(self, "未選取任務", "請先選取要開始的隊列項目")
            return
        changed = 0
        for row in rows:
            job = self.jobs[row]
            if job["status"] == "done":
                continue
            if self.thread and self.thread.isRunning():
                self.thread.request_job_start(row)
            else:
                job.setdefault("stop_event", threading.Event()).clear()
                job["status"] = "pending"
            self.refresh_row(row, "pending")
            changed += 1
        if changed:
            self.log.append(f"已將 {changed} 個任務設為等待，會由下載隊列開始。")

    def stop_selected(self):
        rows = self.selected_rows()
        if not rows:
            QMessageBox.information(self, "未選取任務", "請先選取要停止的隊列項目")
            return
        changed = 0
        for row in rows:
            job = self.jobs[row]
            if job["status"] in ("done", "failed", "stopped", "removed"):
                continue
            if self.thread and self.thread.isRunning():
                self.thread.request_job_stop(row)
            else:
                job["status"] = "stopped"
            self.refresh_row(row, "stopped" if job["status"] != "running" else "running")
            changed += 1
        if changed:
            self.log.append(f"已要求停止 {changed} 個任務。")

    def reset_failed(self):
        for row, job in enumerate(self.jobs):
            if job["status"] in ("failed", "stopped"):
                job["status"] = "pending"
                item = self.job_item(row)
                if item:
                    item.setText(0, self.job_text(job))
        self.save_queue()

    def remove_completed(self):
        """移除已完成任務；由底部往上刪除以保持其他 row 索引正確。"""
        if self.thread and self.thread.isRunning():
            QMessageBox.information(self, "下載進行中", "請等下載停止或完成後再移除已完成任務。")
            return
        rows = [row for row, job in enumerate(self.jobs) if job["status"] in ("done", "removed")]
        for row in reversed(rows):
            self.jobs.pop(row)
            self.queue_list.takeTopLevelItem(row)
        if rows:
            self.log.append(f"已移除 {len(rows)} 個完成任務。")

    def purge_removed(self):
        """工作執行緒結束後真正移除下載途中標記刪除的項目。"""
        rows = [row for row, job in enumerate(self.jobs) if job["status"] == "removed"]
        for row in reversed(rows):
            self.jobs.pop(row)
            self.queue_list.takeTopLevelItem(row)

    def refresh_row(self, row, status):
        self.jobs[row]["status"] = status
        item = self.job_item(row)
        if item:
            item.setText(0, self.job_text(self.jobs[row]))
            if status in ("running", "failed", "stopped"):
                item.setExpanded(True)
        if status == "done":
            self.record_history(self.jobs[row])
        self.save_queue()

    def record_history(self, job):
        try:
            history = json.loads(self.history_file.read_text(encoding="utf-8"))
            if not isinstance(history, list):
                history = []
        except (OSError, ValueError):
            history = []
        if any(item.get("id") == job.get("id") for item in history):
            return
        history.append({"id": job.get("id"), "url": job["url"], "title": job.get("title", ""),
                        "site_key": job.get("site_key", ""), "completed_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        try:
            self.history_file.write_text(json.dumps(history[-500:], ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def show_history(self):
        try:
            history = json.loads(self.history_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            history = []
        dlg = QDialog(self)
        dlg.setWindowTitle("下載歷史")
        dlg.resize(700, 420)
        v = QVBoxLayout()
        editor = QTextEdit()
        editor.setReadOnly(True)
        editor.setPlainText("\n".join(
            f"{item.get('completed_at', '')} | {item.get('site_key', '')} | "
            f"{item.get('title') or '(自動書名)'} | {item.get('url', '')}"
            for item in reversed(history if isinstance(history, list) else [])
        ) or "尚無完成下載紀錄。")
        v.addWidget(editor)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(dlg.accept)
        v.addWidget(close_btn)
        dlg.setLayout(v)
        dlg.exec()

    def clear_cache(self):
        if QMessageBox.question(self, "確認清理快取", f"確定刪除所有快取？\n{cache_root()}") != QMessageBox.StandardButton.Yes:
            return
        try:
            root = cache_root()
            if root.exists():
                for child in root.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            self.log.append("已清理下載快取。")
        except OSError as exc:
            QMessageBox.warning(self, "清理失敗", str(exc))

    def notify(self, title, message):
        if self.tray:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
        QApplication.beep()

    def add_job_log(self, row, msg):
        item = self.job_item(row)
        if not item:
            return
        item.addChild(QTreeWidgetItem([msg]))
        item.setExpanded(True)

    def show_queue_menu(self, pos):
        item = self.queue_list.itemAt(pos)
        if item and item.parent():
            item = item.parent()
        row = self.queue_list.indexOfTopLevelItem(item) if item else -1
        if row < 0:
            return
        menu = QMenu(self)
        copy_url = menu.addAction("複製網址")
        if menu.exec(self.queue_list.viewport().mapToGlobal(pos)) == copy_url:
            QApplication.clipboard().setText(self.jobs[row]["url"])

    def edit_rules(self):
        """編輯自訂過濾規則:全局或網站專用,儲存後下一次下載生效。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("過濾規則編輯")
        dlg.resize(560, 480)
        v = QVBoxLayout()

        # 網站選擇
        site_layout = QHBoxLayout()
        site_layout.addWidget(QLabel("規則類型:"))
        site_combo = QComboBox()
        site_combo.addItem("全局規則 (所有網站)", None)
        site_combo.addItem("69shuba 專用", "69shuba.com")
        site_combo.addItem("sunzhinan 專用", "sunzhinan.com")
        site_combo.addItem("xbanxia 專用", "xbanxia.cc")
        site_combo.addItem("czbooks 專用", "czbooks.net")
        site_layout.addWidget(site_combo)
        site_layout.addStretch()
        v.addLayout(site_layout)

        # 說明
        v.addWidget(QLabel(
            "每行一條規則,命中的整段會從輸出移除:\n"
            "  直接寫文字 = 段落包含該文字就移除;re: 開頭 = 正則;# 開頭 = 註解\n"
            "儲存後下一次下載生效;已下載的書重跑一次即可(用快取,不會重抓)"))

        # 編輯框
        editor = QTextEdit()
        v.addWidget(editor)

        # 規則檔同步
        def load_rules_for_site(site_hint):
            path = rules_path(site_hint)
            if path.exists():
                editor.setPlainText(path.read_text(encoding="utf-8"))
            else:
                header = f"# {site_hint} 專用規則\n\n" if site_hint else "# 全局規則\n\n"
                editor.setPlainText(header)

        def save_and_close():
            site_hint = site_combo.currentData()
            path = ensure_rules_file(site_hint)
            path.write_text(editor.toPlainText(), encoding="utf-8")
            dlg.accept()

        site_combo.currentIndexChanged.connect(
            lambda: load_rules_for_site(site_combo.currentData()))
        load_rules_for_site(None)  # 初始化全局規則

        # 按鈕
        h = QHBoxLayout()
        h.addStretch()
        save_btn = QPushButton("儲存")
        cancel_btn = QPushButton("取消")
        save_btn.clicked.connect(save_and_close)
        cancel_btn.clicked.connect(dlg.reject)
        h.addWidget(save_btn)
        h.addWidget(cancel_btn)
        v.addLayout(h)

        dlg.setLayout(v)
        dlg.exec()

    def edit_adapters(self):
        """匯入或產生 user adapter plugin。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("Adapter 工具")
        dlg.resize(640, 520)
        v = QVBoxLayout()

        v.addWidget(QLabel(
            "貼一個網站目錄網址即可產生 adapter 檔；產生後重啟程式會自動載入。\n"
            "若通用解析不夠準，可編輯 user_adapters 裡的 .py 檔再匯入。"))

        remote_form = QHBoxLayout()
        remote_form.addWidget(QLabel("遠端 .py URL:"))
        remote_url_input = QLineEdit()
        remote_url_input.setPlaceholderText("可貼 GitHub raw adapter URL")
        remote_form.addWidget(remote_url_input)
        v.addLayout(remote_form)

        form = QHBoxLayout()
        form.addWidget(QLabel("網址:"))
        url_input = QLineEdit()
        url_input.setPlaceholderText("https://example.com/book/123/")
        form.addWidget(url_input)
        form.addWidget(QLabel("名稱:"))
        name_input = QLineEdit()
        name_input.setPlaceholderText("可空，例如 my_site")
        name_input.setMaximumWidth(160)
        form.addWidget(name_input)
        v.addLayout(form)

        info = QTextEdit()
        info.setReadOnly(True)
        v.addWidget(info)
        adapter_combo = QComboBox()
        v.addWidget(adapter_combo)

        def refresh_info(extra=""):
            lines = [f"Adapter 資料夾: {user_adapter_dir()}"]
            files = list_user_adapter_files()
            adapter_combo.clear()
            for path in files:
                state = "啟用" if adapter_is_enabled(path) else "停用"
                adapter_combo.addItem(f"{path.name} ({state})", str(path))
            lines.append("")
            lines.append("已匯入/產生的 user adapters:")
            if files:
                lines.extend(f"  - {p.name}" for p in files)
            else:
                lines.append("  (尚無)")
            lines.append("")
            lines.append("目前已載入的 adapter domains:")
            for cls in ADAPTERS:
                source = getattr(cls, "adapter_source", "內建")
                lines.append(f"  - {cls.__name__}: {', '.join(cls.domains)} [{source}]")
            if USER_ADAPTER_ERRORS:
                lines.append("")
                lines.append("載入錯誤:")
                lines.extend(f"  - {err}" for err in USER_ADAPTER_ERRORS)
            if extra:
                lines.append("")
                lines.append(extra)
            info.setPlainText("\n".join(lines))

        def generate_adapter():
            url = url_input.text().strip()
            if not url:
                QMessageBox.warning(self, "缺少網址", "請貼上網站目錄頁 URL")
                return
            try:
                path = write_generated_adapter(url, name_input.text().strip())
            except Exception as exc:
                QMessageBox.warning(self, "產生失敗", str(exc))
                return
            refresh_info(f"已產生: {path}\n請重啟程式後使用新 adapter。")

        def import_adapter():
            path, _ = QFileDialog.getOpenFileName(self, "選擇 Adapter .py", str(Path.home()), "Python (*.py)")
            if not path:
                return
            try:
                target = install_adapter_file(path)
            except Exception as exc:
                QMessageBox.warning(self, "匯入失敗", str(exc))
                return
            refresh_info(f"已匯入: {target}\n請重啟程式後使用新 adapter。")

        def update_remote_adapter():
            remote_url = remote_url_input.text().strip()
            if not remote_url or not remote_url.lower().split("?")[0].endswith(".py"):
                QMessageBox.warning(self, "網址錯誤", "請貼上 .py adapter 的 raw URL")
                return
            try:
                source = Fetcher(encoding="utf-8", delay=0).get(remote_url, retries=2)
                filename = Path(urlparse(remote_url).path).name
                target = user_adapter_dir() / filename
                user_adapter_dir().mkdir(parents=True, exist_ok=True)
                target.write_text(source, encoding="utf-8")
                reload_adapter_files()
                refresh_info(f"已更新遠端 adapter：{target}")
            except Exception as exc:
                QMessageBox.warning(self, "更新失敗", str(exc))

        def test_adapter_url():
            url = url_input.text().strip()
            if not url:
                QMessageBox.warning(self, "缺少網址", "請先貼上要測試的目錄網址")
                return
            try:
                adapter = get_adapter(url)
                fetcher = Fetcher(encoding=adapter.encoding, delay=0)
                catalog_url = adapter.catalog_url(url)
                meta = adapter.meta_url(url)
                if meta:
                    fetcher.get(meta, retries=1)
                book = adapter.parse_catalog(fetcher.get(catalog_url, retries=1))
                if not book.chapters:
                    raise ValueError("找不到章節連結")
                first = book.chapters[0]
                chapter_html = fetcher.get(first.url, retries=1)
                source_url = adapter.chapter_source_url(chapter_html, first.url)
                if source_url:
                    chapter_html = fetcher.get(source_url, referer=first.url, retries=1)
                content = adapter.parse_chapter(chapter_html, title=first.title)
                refresh_info(
                    f"測試成功：{book.title} / 作者 {book.author or '(未解析)'}\n"
                    f"找到 {len(book.chapters)} 章；第一章：{first.title}\n"
                    f"正文預覽：{content[:300]}"
                )
            except Exception as exc:
                refresh_info(f"測試失敗：{type(exc).__name__}: {exc}")

        def reload_adapter_files():
            global USER_ADAPTER_ERRORS
            reload_adapters()
            from sites import USER_ADAPTER_ERRORS as latest_errors
            USER_ADAPTER_ERRORS = latest_errors
            refresh_info("已重新載入 user_adapters；目前 GUI 立即可測試新規則。")

        buttons = QHBoxLayout()
        generate_btn = QPushButton("產生 Adapter")
        generate_btn.clicked.connect(generate_adapter)
        import_btn = QPushButton("匯入 .py Adapter")
        import_btn.clicked.connect(import_adapter)
        remote_btn = QPushButton("更新遠端 Adapter")
        remote_btn.clicked.connect(update_remote_adapter)
        test_btn = QPushButton("測試網址")
        test_btn.clicked.connect(test_adapter_url)
        reload_btn = QPushButton("立即重新載入")
        reload_btn.clicked.connect(reload_adapter_files)
        toggle_btn = QPushButton("切換啟用/停用")
        def toggle_selected_adapter():
            path = adapter_combo.currentData()
            if not path:
                return
            toggle_adapter_enabled(path)
            reload_adapter_files()
        toggle_btn.clicked.connect(toggle_selected_adapter)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(dlg.accept)
        buttons.addWidget(generate_btn)
        buttons.addWidget(import_btn)
        buttons.addWidget(remote_btn)
        buttons.addWidget(test_btn)
        buttons.addWidget(reload_btn)
        buttons.addWidget(toggle_btn)
        buttons.addStretch()
        buttons.addWidget(close_btn)
        v.addLayout(buttons)

        refresh_info()
        dlg.setLayout(v)
        dlg.exec()

    # --- 控制 ---
    def choose_directory(self):
        d = QFileDialog.getExistingDirectory(self, "選擇儲存位置", str(self.selected_dir))
        if d:
            self.selected_dir = Path(d)
            self.dir_label.setText(d)

    def start_queue(self):
        if not any(j["status"] == "pending" for j in self.jobs):
            QMessageBox.information(self, "隊列是空的", "請先「加入隊列」至少一本書")
            return
        try:
            delay = float(self.delay_input.text())
        except ValueError:
            QMessageBox.warning(self, "輸入錯誤", "延遲必須是數字")
            return
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.queue_list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.remove_btn.setEnabled(True)
        self.retry_btn.setEnabled(False)
        self.concurrent_input.setEnabled(False)
        self.site_concurrent_input.setEnabled(False)
        self.retry_input.setEnabled(False)
        self.output_format_input.setEnabled(False)
        self.filename_format_input.setEnabled(False)
        self.progress.setValue(0)
        workers = self.concurrent_input.value()
        site_workers = self.site_concurrent_input.value()
        retries = self.retry_input.value()
        output_format = self.output_format_input.currentData()
        filename_format = self.filename_format_input.currentData()
        self.log.append(f"—— 開始處理隊列，同時下載 {workers} 本；同網站最多 {site_workers} 本 ——")
        self.thread = QueueThread(self.jobs, self.selected_dir, delay, workers, site_workers,
                                  retries, output_format, self.site_settings, filename_format,
                                  self.site_cookies)
        self.thread.sig_log.connect(self.log.append)
        self.thread.sig_job_log.connect(self.add_job_log)
        self.thread.sig_progress.connect(self.on_progress)
        self.thread.sig_stats.connect(self.on_stats)
        self.thread.sig_job_status.connect(self.refresh_row)
        self.thread.sig_all_done.connect(self.on_all_done)
        self.thread.start()

    def stop_queue(self):
        if self.thread:
            self.thread.request_stop()
            self.log.append("正在停止所有下載(等各自目前章節抓完)...")
            self.stop_btn.setEnabled(False)

    def on_progress(self, row, current, total):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(current)
        self.progress.setFormat(f"隊列 {row + 1}：%v / %m 章")

    def on_stats(self, row, current, total):
        self.chapter_progress[row] = (current, total)
        elapsed = max(time.monotonic() - self.stats_started, 0.001)
        completed = sum(current for current, _ in self.chapter_progress.values())
        total_chapters = sum(total for _, total in self.chapter_progress.values())
        rate = completed / elapsed
        remaining = max(total_chapters - completed, 0)
        eta = remaining / rate if rate > 0 else 0
        self.progress_info.setText(
            f"速度：{rate:.2f} 章/秒 ｜ 預估剩餘：{eta / 60:.1f} 分鐘")

    def on_all_done(self):
        self.purge_removed()
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.queue_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.remove_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)
        self.concurrent_input.setEnabled(True)
        self.site_concurrent_input.setEnabled(True)
        self.retry_input.setEnabled(True)
        self.output_format_input.setEnabled(True)
        self.filename_format_input.setEnabled(True)
        self.log.append("—— 隊列處理結束 ——")
        self.notify("小說下載器", "下載隊列處理結束")
        self.save_queue()

    def save_queue(self):
        """保存可恢復的隊列資料，不寫入執行緒 Event 等執行期物件。"""
        try:
            data = []
            for job in self.jobs:
                row = {key: job.get(key) for key in ("id", "url", "title", "start", "end", "status", "site_key")}
                if row["status"] == "running":
                    row["status"] = "stopped"
                data.append(row)
            self.queue_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def filter_queue(self, text):
        needle = text.strip().lower()
        for row, job in enumerate(self.jobs):
            item = self.job_item(row)
            if item:
                item.setHidden(bool(needle) and needle not in self.job_text(job).lower())

    def export_queue(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "匯出下載隊列", str(Path.home() / "novelDownloader-queue.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            rows = []
            for job in self.jobs:
                row = {key: job.get(key) for key in ("id", "url", "title", "start", "end", "status", "site_key")}
                if row["status"] in ("running", "removing"):
                    row["status"] = "stopped"
                rows.append(row)
            Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log.append(f"隊列已匯出：{path}")
        except OSError as exc:
            QMessageBox.warning(self, "匯出失敗", str(exc))

    def import_queue(self):
        if self.thread and self.thread.isRunning():
            QMessageBox.information(self, "下載進行中", "請等隊列停止或完成後再匯入隊列。")
            return
        path, _ = QFileDialog.getOpenFileName(self, "匯入下載隊列", str(Path.home()), "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("JSON 必須是隊列陣列")
            existing = {job["url"] for job in self.jobs}
            added = 0
            for row in data:
                if not isinstance(row, dict) or not row.get("url") or row["url"] in existing:
                    continue
                job = {
                    "url": row["url"], "title": row.get("title", ""),
                    "start": row.get("start"), "end": row.get("end"),
                    "status": row.get("status", "pending"),
                    "site_key": row.get("site_key") or site_key_for_url(row["url"]),
                    "id": row.get("id") or uuid.uuid4().hex,
                }
                if job["status"] in ("running", "removing", "removed"):
                    job["status"] = "stopped" if job["status"] != "removed" else "removed"
                if job["status"] not in STATUS_LABEL:
                    job["status"] = "pending"
                job.setdefault("id", uuid.uuid4().hex)
                self.jobs.append(job)
                self.queue_list.addTopLevelItem(self.make_job_item(job))
                existing.add(job["url"])
                added += 1
            self.save_queue()
            self.log.append(f"已匯入 {added} 個隊列項目。")
        except (OSError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "匯入失敗", str(exc))

    def load_site_settings(self):
        try:
            data = json.loads(self.site_settings_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def edit_cookies(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Cookie 匯入（本次執行有效）")
        dlg.resize(620, 280)
        v = QVBoxLayout()
        v.addWidget(QLabel("輸入網站網域；可從 Chrome 讀取 Cookie，或手動貼上 Cookie header。Cookie 不會寫入檔案。"))
        form = QHBoxLayout()
        form.addWidget(QLabel("網域:"))
        domain = QLineEdit()
        domain.setPlaceholderText("例如 xbanxia.cc")
        form.addWidget(domain)
        v.addLayout(form)
        editor = QTextEdit()
        editor.setPlaceholderText("name=value; name2=value2")
        v.addWidget(editor)
        info = QLabel("")
        v.addWidget(info)
        buttons = QHBoxLayout()
        browser_btn = QPushButton("匯入 Chrome Cookie")
        open_btn = QPushButton("開啟瀏覽器")
        save_btn = QPushButton("套用")
        close_btn = QPushButton("關閉")
        buttons.addWidget(browser_btn); buttons.addWidget(open_btn); buttons.addWidget(save_btn); buttons.addStretch(); buttons.addWidget(close_btn)
        v.addLayout(buttons)

        def import_chrome():
            host = domain.text().strip()
            if not host:
                info.setText("請先輸入網域")
                return
            try:
                import browser_cookie3
                cookies = browser_cookie3.chrome(domain_name=host)
                header = "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies)
                if not header:
                    raise RuntimeError("Chrome 找不到此網域的 Cookie")
                editor.setPlainText(header)
                info.setText(f"已讀取 {len(header.split('; '))} 個 Cookie；按套用後本次下載有效")
            except Exception as exc:
                info.setText(f"讀取失敗：{exc}")

        def apply_cookie():
            host = domain.text().strip().lower()
            value = editor.toPlainText().strip()
            if not host or not value:
                info.setText("網域與 Cookie 都不可為空")
                return
            self.site_cookies[host] = value
            info.setText(f"已套用 {host} Cookie")

        def open_browser():
            host = domain.text().strip()
            if host:
                QDesktopServices.openUrl(QUrl("https://" + host))

        browser_btn.clicked.connect(import_chrome)
        open_btn.clicked.connect(open_browser)
        save_btn.clicked.connect(apply_cookie)
        close_btn.clicked.connect(dlg.accept)
        dlg.setLayout(v)
        dlg.exec()

    def edit_site_settings(self):
        """編輯各網站的覆寫值：每行 domain | delay | max_workers。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("網站獨立設定")
        dlg.resize(620, 420)
        v = QVBoxLayout()
        v.addWidget(QLabel(
            "每行格式：domain | 延遲秒數 | 同網站最多 | User-Agent(可空) | Referer(可空)\n"
            "例如：69shuba.tw | 3 | 1 | Mozilla/5.0 | https://69shuba.tw/"))
        editor = QTextEdit()
        domains = sorted(set(self.site_settings) | {
            self.job_site_label(job) for job in self.jobs
        })
        lines = []
        for domain in domains:
            item = self.site_settings.get(domain, {})
            lines.append(f"{domain} | {item.get('delay', self.delay_input.text() or 2.0)} | "
                         f"{item.get('max_workers', self.site_concurrent_input.value())} | "
                         f"{item.get('user_agent', '')} | {item.get('referer', '')}")
        editor.setPlainText("\n".join(lines))
        v.addWidget(editor)
        buttons = QHBoxLayout()
        save_btn = QPushButton("儲存")
        cancel_btn = QPushButton("取消")
        buttons.addStretch(); buttons.addWidget(save_btn); buttons.addWidget(cancel_btn)
        v.addLayout(buttons)

        def save():
            settings = {}
            for line in editor.toPlainText().splitlines():
                parts = [part.strip() for part in line.split("|")]
                if not line.strip():
                    continue
                if len(parts) not in (3, 5) or not parts[0]:
                    QMessageBox.warning(dlg, "格式錯誤", f"無法解析：{line}")
                    return
                try:
                    delay = max(0.0, float(parts[1]))
                    max_workers = max(1, int(parts[2]))
                except ValueError:
                    QMessageBox.warning(dlg, "格式錯誤", f"延遲或並行數不是數字：{line}")
                    return
                settings[parts[0]] = {"delay": delay, "max_workers": max_workers}
                if len(parts) == 5:
                    settings[parts[0]].update({"user_agent": parts[3], "referer": parts[4]})
            self.site_settings = settings
            try:
                self.site_settings_file.write_text(
                    json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(dlg, "儲存失敗", str(exc))
                return
            dlg.accept()

        save_btn.clicked.connect(save)
        cancel_btn.clicked.connect(dlg.reject)
        dlg.setLayout(v)
        dlg.exec()

    def load_queue(self):
        """啟動時恢復上次未完成的隊列。"""
        try:
            data = json.loads(self.queue_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            job = {
                "url": row["url"], "title": row.get("title", ""),
                "start": row.get("start"), "end": row.get("end"),
                "status": row.get("status", "pending"),
                "site_key": row.get("site_key") or site_key_for_url(row["url"]),
                "id": row.get("id") or uuid.uuid4().hex,
            }
            if job["status"] == "running":
                job["status"] = "stopped"
            if job["status"] not in STATUS_LABEL:
                job["status"] = "pending"
            job.setdefault("id", uuid.uuid4().hex)
            self.jobs.append(job)
            self.queue_list.addTopLevelItem(self.make_job_item(job))


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NovelDownloaderUI()
    window.show()
    sys.exit(app.exec())
