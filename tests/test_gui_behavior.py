import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from fanqie_preview import FanqiePreviewDialog
import fanqie_preview
import main_window
from sites.fanqie import FanqieChapter
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
    monkeypatch.setenv("NOVELDOWNLOADER_DATA_DIR", str(tmp_path))
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


def test_fanqie_preview_is_exposed_and_not_queued_for_txt(monkeypatch, tmp_path):
    app, window = make_window(monkeypatch, tmp_path)
    messages = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: messages.append(args[2]))
    try:
        assert window.fanqie_preview_btn.text() == "番茄小說：預覽支援"
        window.url_input.setText("https://fanqienovel.com/page/123456789")
        window.add_btn.click()
        app.processEvents()
        assert window.jobs == []
        assert messages and "不能加入 TXT／EPUB" in messages[0]
    finally:
        window.close()


def test_fanqie_preview_dialog_directory_and_selection_controls(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELDOWNLOADER_DATA_DIR", str(tmp_path / "profile"))
    app = QApplication.instance() or QApplication([])
    dialog = FanqiePreviewDialog("https://fanqienovel.com/page/123456789")
    try:
        dialog.book_id = "123456789"
        dialog.chapters = [
            FanqieChapter("100", "第一章", "第一卷", 1, {
                "needPay": 0, "isPaidPublication": False,
                "isPaidStory": False, "isChapterLock": False,
            }),
            FanqieChapter("200", "狀態不明章", "第一卷", 2, {
                "needPay": 0, "isChapterLock": True,
            }),
        ]
        dialog._populate_directory()
        assert dialog.tree.topLevelItemCount() == 2
        assert dialog.tree.topLevelItem(0).text(0) == "第一卷"
        assert dialog.tree.topLevelItem(0).text(2) == "第一章"
        assert dialog.tree.topLevelItem(0).text(3).startswith("公開")
        assert not dialog.tree.topLevelItem(1).isDisabled()
        assert dialog.raw_state.text() == "原始資料：尚未保存"
        assert dialog.reader_state.text() == "字型閱讀預覽：不可用（尚未匯入）"
        assert dialog.text_state.text() == "文字尚未還原"
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        app.processEvents()
        assert dialog.save_selected_button.isEnabled()
        assert not dialog.preview_button.isEnabled()
        assert dialog.import_reader_button.isEnabled()
        assert not dialog.reading_preview_button.isEnabled()
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(1))
        app.processEvents()
        assert not dialog.save_selected_button.isEnabled()
        assert dialog.original_button.isEnabled()
        assert not dialog.import_reader_button.isEnabled()
        assert "視覺預覽不代表文字已還原" in dialog.warning.text()
    finally:
        dialog.close()


def test_reader_html_import_cancel_keeps_chapter_and_cache_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELDOWNLOADER_DATA_DIR", str(tmp_path / "profile"))
    app = QApplication.instance() or QApplication([])
    dialog = FanqiePreviewDialog("https://fanqienovel.com/page/123456789")
    try:
        dialog.book_id = "123456789"
        dialog.chapters = [FanqieChapter("100", "第一章", "第一卷", 1, {
            "needPay": 0, "isPaidPublication": False,
            "isPaidStory": False, "isChapterLock": False,
        })]
        dialog._populate_directory()
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        dialog._choose_reader_source = lambda: ""

        dialog.import_reader_page()

        assert dialog.tree.currentItem().text(2) == "第一章"
        assert not (dialog.cache_root / "123456789" / "100").exists()
        assert dialog.import_reader_button.isEnabled()
        assert not dialog.reading_preview_button.isEnabled()
    finally:
        dialog.close()


def test_fanqie_selection_validates_font_once(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELDOWNLOADER_DATA_DIR", str(tmp_path / "profile"))
    app = QApplication.instance() or QApplication([])
    dialog = FanqiePreviewDialog()
    try:
        dialog.book_id = "123456789"
        dialog.chapters = [FanqieChapter("100", "第一章", "第一卷", 1, {
            "needPay": 0, "isPaidPublication": False,
            "isPaidStory": False, "isChapterLock": False,
        })]
        dialog._populate_directory()
        dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0))
        app.processEvents()
        calls = []
        monkeypatch.setattr(fanqie_preview, "reading_preview_status",
                            lambda *args: (calls.append(args) or (True, "ok")))
        dialog.update_buttons()
        assert len(calls) == 1
        assert dialog.reading_preview_button.isEnabled()
        assert not dialog.import_reader_button.isEnabled()
    finally:
        dialog.close()


def test_loading_new_fanqie_url_clears_stale_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELDOWNLOADER_DATA_DIR", str(tmp_path / "profile"))
    app = QApplication.instance() or QApplication([])
    dialog = FanqiePreviewDialog("https://fanqienovel.com/page/123456789")
    started = []
    try:
        dialog.book_id = "123456789"
        dialog.chapters = [FanqieChapter("100", "第一章", "第一卷", 1, {
            "needPay": 0, "isPaidPublication": False,
            "isPaidStory": False, "isChapterLock": False,
        })]
        dialog._populate_directory()
        monkeypatch.setattr(dialog, "_start", lambda worker: started.append(worker))
        dialog.url_input.setText("https://fanqienovel.com/page/987654321")

        dialog.load_directory()

        assert len(started) == 1 and started[0].mode == "directory"
        assert dialog.book_id == ""
        assert dialog.chapters == []
        assert dialog.tree.topLevelItemCount() == 0
        assert dialog.info.text().startswith("目錄尚未載入")
        assert not dialog.save_first_button.isEnabled()
        assert not dialog.original_button.isEnabled()
    finally:
        dialog.close()


def test_job_logs_are_separated_per_queue(monkeypatch, tmp_path):
    app, window = make_window(monkeypatch, tmp_path)
    try:
        for url, title in (("https://example.com/book/1", "甲書"),
                           ("https://example.com/book/2", "乙書")):
            window.url_input.setText(url)
            window.title_input.setText(title)
            window.add_btn.click()
        app.processEvents()
        window.add_job_log(0, "[1/10] 第一章")
        window.add_job_log(1, "[1/20] 第一章")
        window.add_job_log(0, "[2/10] 第二章")
        app.processEvents()

        assert window.log_tabs.count() == 3
        assert window.log_tabs.tabText(1) == "隊列 1｜甲書"
        assert window.log_tabs.tabText(2) == "隊列 2｜乙書"
        first = window.log_tabs.widget(1).toPlainText()
        second = window.log_tabs.widget(2).toPlainText()
        assert "[1/10] 第一章" in first and "[2/10] 第二章" in first
        assert "[1/20]" not in first
        assert second.strip() == "[1/20] 第一章"
        assert "第一章" not in window.log.toPlainText()

        window.clear_job_logs()
        app.processEvents()
        assert window.log_tabs.count() == 1
    finally:
        window.close()


def test_job_log_tabs_track_title_and_removal(monkeypatch, tmp_path):
    app, window = make_window(monkeypatch, tmp_path)
    try:
        for url in ("https://example.com/book/1", "https://example.com/book/2"):
            window.url_input.setText(url)
            window.add_btn.click()
        app.processEvents()
        window.add_job_log(0, "《異度旅社》作者: 疊瞳,全書 894 章")
        window.add_job_log(1, "正在抓取目錄...")
        app.processEvents()
        assert window.log_tabs.tabText(1) == "隊列 1｜異度旅社"
        assert window.log_tabs.tabText(2) == "隊列 2"

        window.queue_list.setCurrentItem(window.queue_list.topLevelItem(0))
        window.remove_selected()
        app.processEvents()
        assert window.log_tabs.count() == 2
        assert window.log_tabs.tabText(1) == "隊列 1"  # 原隊列 2 重新編號
    finally:
        window.close()


class FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


def test_browser_cookie_import_falls_back_to_next_browser():
    """Chrome 需要管理員權限時，改用下一個瀏覽器，而不是整個失敗。"""

    class FakeModule:
        @staticmethod
        def chrome(domain_name):
            raise RuntimeError("This operation requires admin. Please run as admin.")

        @staticmethod
        def edge(domain_name):
            return []

        @staticmethod
        def firefox(domain_name):
            return [FakeCookie("cf_clearance", "abc"), FakeCookie("sid", "1")]

    label, header, problems = main_window.read_browser_cookies("69shuba.tw", FakeModule)
    assert label == "Firefox"
    assert header == "cf_clearance=abc; sid=1"
    assert problems[0] == "Chrome：新版 Chrome/Edge 的 Cookie 加密需要系統管理員權限"
    assert problems[1] == "Edge：沒有此網域的 Cookie"


def test_browser_cookie_import_reports_every_reason_when_all_fail():
    class FakeModule:
        @staticmethod
        def chrome(domain_name):
            raise RuntimeError("This operation requires admin. Please run as admin.")

        @staticmethod
        def firefox(domain_name):
            raise RuntimeError("Could not find Firefox profile directory")

    label, header, problems = main_window.read_browser_cookies("69shuba.tw", FakeModule)
    assert (label, header) == ("", "")
    assert problems == [
        "Chrome：新版 Chrome/Edge 的 Cookie 加密需要系統管理員權限",
        "Firefox：沒有安裝或找不到設定檔",
    ]


def test_cookie_header_only_keeps_matching_domain():
    from chrome_cookies import cookies_to_header

    cookies = [
        {"domain": ".69shuba.tw", "name": "cf_clearance", "value": "abc"},
        {"domain": "69shuba.tw", "name": "sid", "value": "1"},
        {"domain": ".other.com", "name": "ad", "value": "x"},
    ]
    assert cookies_to_header(cookies, "69shuba.tw") == "cf_clearance=abc; sid=1"
    assert cookies_to_header(cookies, "www.69shuba.tw") == "cf_clearance=abc; sid=1"
    assert cookies_to_header(cookies, "other.com") == "ad=x"
    assert cookies_to_header(cookies, "example.test") == ""
