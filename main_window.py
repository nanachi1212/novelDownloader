"""PyQt6 圖形界面:下載隊列、章節範圍、儲存位置、書名編輯、進度與日誌。"""
import sys
from pathlib import Path

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
    QListWidget,
    QFileDialog,
    QMessageBox,
    QSpinBox,
    QDialog,
)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QFont

from downloader_task import Cancelled, download_novel
from textfilter import ensure_rules_file, rules_path
from PyQt6.QtWidgets import QComboBox

STATUS_LABEL = {
    "pending": "⏳ 等待",
    "running": "▶ 下載中",
    "done": "✓ 完成",
    "failed": "✗ 失敗",
    "stopped": "⏸ 已停止(可續傳)",
}


class QueueThread(QThread):
    """依序處理隊列;jobs 是與主執行緒共享的 list,執行中允許繼續加入。"""

    sig_log = pyqtSignal(str)
    sig_progress = pyqtSignal(int, int)
    sig_job_status = pyqtSignal(int, str)
    sig_all_done = pyqtSignal()

    def __init__(self, jobs, output_dir, delay):
        super().__init__()
        self.jobs = jobs
        self.output_dir = output_dir
        self.delay = delay
        self.stop_requested = False

    def run(self):
        while not self.stop_requested:
            row = next((i for i, j in enumerate(self.jobs) if j["status"] == "pending"), None)
            if row is None:
                break
            job = self.jobs[row]
            job["status"] = "running"
            self.sig_job_status.emit(row, "running")
            try:
                def cb(stage, current, total, msg):
                    if stage == "chapter":
                        self.sig_progress.emit(current, total)
                        if current == 1 or current == total or current % 10 == 0:
                            self.sig_log.emit(msg)
                    else:
                        self.sig_log.emit(msg)

                download_novel(
                    job["url"], self.output_dir, job["title"], self.delay, cb,
                    start=job["start"], end=job["end"],
                    cancel_check=lambda: self.stop_requested,
                )
                job["status"] = "done"
                self.sig_job_status.emit(row, "done")
            except Cancelled:
                job["status"] = "stopped"
                self.sig_job_status.emit(row, "stopped")
                self.sig_log.emit("已停止。再按「開始下載」會從快取續傳。")
            except Exception as e:
                job["status"] = "failed"
                self.sig_job_status.emit(row, "failed")
                self.sig_log.emit(f"✗ 這本失敗,繼續下一本: {e}")
        self.sig_all_done.emit()


class NovelDownloaderUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("小說下載器")
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
        layout.addWidget(QLabel("下載隊列(依序執行,下載中也可以繼續加):"))
        self.queue_list = QListWidget()
        self.queue_list.setMaximumHeight(140)
        layout.addWidget(self.queue_list)
        qbtn_layout = QHBoxLayout()
        self.remove_btn = QPushButton("移除選中")
        self.remove_btn.clicked.connect(self.remove_selected)
        qbtn_layout.addWidget(self.remove_btn)
        self.retry_btn = QPushButton("失敗/停止的重設為等待")
        self.retry_btn.clicked.connect(self.reset_failed)
        qbtn_layout.addWidget(self.retry_btn)
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
        self.rules_btn = QPushButton("過濾規則...")
        self.rules_btn.clicked.connect(self.edit_rules)
        dir_layout.addWidget(self.rules_btn)
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
        return f"{STATUS_LABEL[job['status']]} | {name} | {rng} | {job['url']}"

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
               "start": start, "end": end, "status": "pending"}
        self.jobs.append(job)
        self.queue_list.addItem(self.job_text(job))
        self.url_input.clear()
        self.title_input.clear()
        self.start_input.clear()
        self.end_input.clear()

    def remove_selected(self):
        row = self.queue_list.currentRow()
        if row < 0:
            return
        if self.jobs[row]["status"] == "running":
            QMessageBox.warning(self, "無法移除", "這本正在下載,請先按「停止」")
            return
        self.jobs.pop(row)
        self.queue_list.takeItem(row)

    def reset_failed(self):
        for row, job in enumerate(self.jobs):
            if job["status"] in ("failed", "stopped"):
                job["status"] = "pending"
                self.queue_list.item(row).setText(self.job_text(job))

    def refresh_row(self, row, status):
        self.jobs[row]["status"] = status
        self.queue_list.item(row).setText(self.job_text(self.jobs[row]))

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
        self.progress.setValue(0)
        self.thread = QueueThread(self.jobs, self.selected_dir, delay)
        self.thread.sig_log.connect(self.log.append)
        self.thread.sig_progress.connect(self.on_progress)
        self.thread.sig_job_status.connect(self.refresh_row)
        self.thread.sig_all_done.connect(self.on_all_done)
        self.thread.start()

    def stop_queue(self):
        if self.thread:
            self.thread.stop_requested = True
            self.log.append("正在停止(等目前章節抓完)...")
            self.stop_btn.setEnabled(False)

    def on_progress(self, current, total):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(current)

    def on_all_done(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.remove_btn.setEnabled(True)
        self.retry_btn.setEnabled(True)
        self.log.append("—— 隊列處理結束 ——")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = NovelDownloaderUI()
    window.show()
    sys.exit(app.exec())
