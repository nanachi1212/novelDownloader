import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

import main_window
from state_io import read_json


def wait_for_queue(path, expected, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = read_json(path, None)
        if current == expected:
            return current
        time.sleep(0.01)
    return read_json(path, None)


def make_window(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_window, "__file__", str(tmp_path / "main_window.py"))
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    window = main_window.NovelDownloaderUI()
    return app, window


def test_gui_add_filter_stop_restart_remove_and_persist(monkeypatch, tmp_path):
    app, window = make_window(monkeypatch, tmp_path)
    try:
        window.url_input.setText("https://example.com/book/1")
        window.title_input.setText("測試小說")
        window.start_input.setText("2")
        window.end_input.setText("8")
        window.add_btn.click()
        app.processEvents()

        assert len(window.jobs) == 1
        assert window.queue_list.topLevelItemCount() == 1
        assert window.jobs[0]["start"] == 2
        expected_job = [{
            "url": "https://example.com/book/1",
            "title": "測試小說",
            "start": 2,
            "end": 8,
            "status": "pending",
            "site_key": "example.com",
            "id": window.jobs[0]["id"],
        }]
        assert wait_for_queue(tmp_path / "queue.json", expected_job) == expected_job

        window.filter_queue("不存在")
        assert window.queue_list.topLevelItem(0).isHidden()
        window.filter_queue("測試小說")
        assert not window.queue_list.topLevelItem(0).isHidden()

        window.queue_list.setCurrentItem(window.queue_list.topLevelItem(0))
        window.stop_selected_btn.click()
        assert window.jobs[0]["status"] == "stopped"
        window.start_selected_btn.click()
        assert window.jobs[0]["status"] == "pending"
        window.remove_btn.click()
        assert window.jobs == []
        assert wait_for_queue(tmp_path / "queue.json", []) == []
    finally:
        window.close()


def test_gui_restores_running_job_as_stopped(monkeypatch, tmp_path):
    (tmp_path / "queue.json").write_text(
        '[{"url":"https://example.com/book","title":"恢復測試","status":"running"}]',
        encoding="utf-8",
    )
    _app, window = make_window(monkeypatch, tmp_path)
    try:
        assert len(window.jobs) == 1
        assert window.jobs[0]["status"] == "stopped"
        assert "已停止" in window.queue_list.topLevelItem(0).text(0)
    finally:
        window.close()


def test_start_queue_automatically_resumes_stopped_job(monkeypatch, tmp_path):
    (tmp_path / "queue.json").write_text(
        '[{"url":"https://example.com/book","title":"恢復測試","status":"stopped"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(main_window, "download_novel", lambda *args, **kwargs: None)
    app, window = make_window(monkeypatch, tmp_path)
    try:
        window.start_btn.click()
        deadline = time.monotonic() + 2
        while window.thread and window.thread.isRunning() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()

        assert window.jobs[0]["status"] == "done"
        assert "已自動恢復 1 個停止/失敗的任務為等待" in window.log.toPlainText()
    finally:
        window.close()


def test_adding_existing_stopped_url_resumes_it(monkeypatch, tmp_path):
    url = "https://example.com/book"
    (tmp_path / "queue.json").write_text(
        json.dumps([{"url": url, "title": "恢復測試", "status": "stopped"}],
                   ensure_ascii=False),
        encoding="utf-8",
    )
    app, window = make_window(monkeypatch, tmp_path)
    try:
        window.url_input.setText(url)
        window.add_btn.click()
        app.processEvents()

        assert len(window.jobs) == 1
        assert window.jobs[0]["status"] == "pending"
        assert "已將停止/失敗的任務恢復為等待" in window.log.toPlainText()
    finally:
        window.close()
