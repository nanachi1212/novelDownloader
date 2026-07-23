"""PyQt6 圖形界面:下載隊列、章節範圍、儲存位置、書名編輯、進度與日誌。"""
import sys
import threading
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
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from downloader_task import Cancelled, download_novel
from adapter_tools import (
    install_adapter_file,
    list_user_adapter_files,
    user_adapter_dir,
    write_generated_adapter,
)
from textfilter import ensure_rules_file, rules_path
from PyQt6.QtWidgets import QComboBox
from sites import ADAPTERS, USER_ADAPTER_ERRORS, get_adapter

APP_VERSION = "1.3.0"

STATUS_LABEL = {
    "pending": "⏳ 等待",
    "running": "▶ 下載中",
    "done": "✓ 完成",
    "failed": "✗ 失敗",
    "stopped": "⏸ 已停止(可續傳)",
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


class QueueThread(QThread):
    """以可設定的工作數並行處理書籍；每本書內的章節仍依序下載。"""

    sig_log = pyqtSignal(str)
    sig_job_log = pyqtSignal(int, str)
    sig_progress = pyqtSignal(int, int, int)
    sig_job_status = pyqtSignal(int, str)
    sig_all_done = pyqtSignal()

    def __init__(self, jobs, output_dir, delay, max_workers=2, max_workers_per_site=6):
        super().__init__()
        self.jobs = jobs
        self.output_dir = output_dir
        self.delay = delay
        self.max_workers = max(1, int(max_workers))
        self.max_workers_per_site = max(1, int(max_workers_per_site))
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
                    if running_by_site.get(site_key, 0) >= self.max_workers_per_site:
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
                        if current == 1 or current == total or current % 10 == 0:
                            self.sig_job_log.emit(row, msg)
                            self.sig_log.emit(f"[{prefix}] {msg}")
                    else:
                        self.sig_job_log.emit(row, msg)
                        self.sig_log.emit(f"[{prefix}] {msg}")

                download_novel(
                    job["url"], self.output_dir, job["title"], self.delay, cb,
                    start=job["start"], end=job["end"],
                    cancel_check=lambda: self._job_cancel_requested(row),
                )
                job["status"] = "done"
                self.sig_job_status.emit(row, "done")
            except Cancelled:
                job["status"] = "stopped"
                self.sig_job_status.emit(row, "stopped")
                self.sig_job_log.emit(row, "已停止；重新開始會從快取續傳。")
                self.sig_log.emit(f"[{prefix}] 已停止；重新開始會從快取續傳。")
            except Exception as e:
                job["status"] = "failed"
                self.sig_job_status.emit(row, "failed")
                self.sig_job_log.emit(row, f"✗ 下載失敗：{e}")
                self.sig_log.emit(f"[{prefix}] ✗ 下載失敗：{e}")

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
        self.queue_list = QTreeWidget()
        self.queue_list.setHeaderHidden(True)
        self.queue_list.setMaximumHeight(140)
        self.queue_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_list.customContextMenuRequested.connect(self.show_queue_menu)
        layout.addWidget(self.queue_list)
        qbtn_layout = QHBoxLayout()
        self.remove_btn = QPushButton("移除選中")
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
        self.rules_btn = QPushButton("過濾規則...")
        self.rules_btn.clicked.connect(self.edit_rules)
        dir_layout.addWidget(self.rules_btn)
        self.adapter_btn = QPushButton("Adapter 工具...")
        self.adapter_btn.clicked.connect(self.edit_adapters)
        dir_layout.addWidget(self.adapter_btn)
        layout.addLayout(dir_layout)

        # --- 進度與日誌 ---
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)
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
        self.jobs.append(job)
        self.queue_list.addTopLevelItem(QTreeWidgetItem([self.job_text(job)]))
        self.url_input.clear()
        self.title_input.clear()
        self.start_input.clear()
        self.end_input.clear()

    def remove_selected(self):
        item = self.queue_list.currentItem()
        if item and item.parent():
            item = item.parent()
        row = self.queue_list.indexOfTopLevelItem(item) if item else -1
        if row < 0:
            return
        if self.jobs[row]["status"] == "running":
            QMessageBox.warning(self, "無法移除", "這本正在下載,請先按「停止」")
            return
        self.jobs.pop(row)
        self.queue_list.takeTopLevelItem(row)

    def selected_row(self):
        item = self.queue_list.currentItem()
        if item and item.parent():
            item = item.parent()
        return self.queue_list.indexOfTopLevelItem(item) if item else -1

    def start_selected(self):
        row = self.selected_row()
        if row < 0:
            QMessageBox.information(self, "未選取任務", "請先選取要開始的隊列項目")
            return
        job = self.jobs[row]
        if job["status"] == "done":
            QMessageBox.information(self, "任務已完成", "這本書已完成；如需重跑可先移除後重新加入。")
            return
        if self.thread and self.thread.isRunning():
            self.thread.request_job_start(row)
        else:
            job.setdefault("stop_event", threading.Event()).clear()
            job["status"] = "pending"
        self.refresh_row(row, "pending")
        self.log.append(f"[{job['title'] or f'隊列 {row + 1}'}] 已設為等待，會由下載隊列開始。")

    def stop_selected(self):
        row = self.selected_row()
        if row < 0:
            QMessageBox.information(self, "未選取任務", "請先選取要停止的隊列項目")
            return
        job = self.jobs[row]
        if job["status"] in ("done", "failed", "stopped"):
            return
        if self.thread and self.thread.isRunning():
            self.thread.request_job_stop(row)
        else:
            job["status"] = "stopped"
        self.refresh_row(row, "stopped" if job["status"] != "running" else "running")
        self.log.append(f"[{job['title'] or f'隊列 {row + 1}'}] 已要求單獨停止。")

    def reset_failed(self):
        for row, job in enumerate(self.jobs):
            if job["status"] in ("failed", "stopped"):
                job["status"] = "pending"
                item = self.job_item(row)
                if item:
                    item.setText(0, self.job_text(job))

    def remove_completed(self):
        """移除已完成任務；由底部往上刪除以保持其他 row 索引正確。"""
        if self.thread and self.thread.isRunning():
            QMessageBox.information(self, "下載進行中", "請等下載停止或完成後再移除已完成任務。")
            return
        rows = [row for row, job in enumerate(self.jobs) if job["status"] == "done"]
        for row in reversed(rows):
            self.jobs.pop(row)
            self.queue_list.takeTopLevelItem(row)
        if rows:
            self.log.append(f"已移除 {len(rows)} 個完成任務。")

    def refresh_row(self, row, status):
        self.jobs[row]["status"] = status
        item = self.job_item(row)
        if item:
            item.setText(0, self.job_text(self.jobs[row]))
            if status in ("running", "failed", "stopped"):
                item.setExpanded(True)

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

        def refresh_info(extra=""):
            lines = [f"Adapter 資料夾: {user_adapter_dir()}"]
            files = list_user_adapter_files()
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

        buttons = QHBoxLayout()
        generate_btn = QPushButton("產生 Adapter")
        generate_btn.clicked.connect(generate_adapter)
        import_btn = QPushButton("匯入 .py Adapter")
        import_btn.clicked.connect(import_adapter)
        close_btn = QPushButton("關閉")
        close_btn.clicked.connect(dlg.accept)
        buttons.addWidget(generate_btn)
        buttons.addWidget(import_btn)
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
        self.remove_btn.setEnabled(False)
        self.retry_btn.setEnabled(False)
        self.concurrent_input.setEnabled(False)
        self.site_concurrent_input.setEnabled(False)
        self.progress.setValue(0)
        workers = self.concurrent_input.value()
        site_workers = self.site_concurrent_input.value()
        self.log.append(f"—— 開始處理隊列，同時下載 {workers} 本；同網站最多 {site_workers} 本 ——")
        self.thread = QueueThread(self.jobs, self.selected_dir, delay, workers, site_workers)
        self.thread.sig_log.connect(self.log.append)
        self.thread.sig_job_log.connect(self.add_job_log)
        self.thread.sig_progress.connect(self.on_progress)
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

    def on_all_done(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.remove_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)
        self.concurrent_input.setEnabled(True)
        self.site_concurrent_input.setEnabled(True)
        self.log.append("—— 隊列處理結束 ——")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NovelDownloaderUI()
    window.show()
    sys.exit(app.exec())
