import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

import main_window
from state_io import read_json


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
        assert read_json(tmp_path / "queue.json", [])[0]["title"] == "測試小說"

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
        assert read_json(tmp_path / "queue.json", None) == []
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
