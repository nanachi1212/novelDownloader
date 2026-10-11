import json
import sys
from pathlib import Path

import pytest

import app_paths


@pytest.mark.parametrize("platform,base", [
    ("win32", "AppData/Local"),
    ("darwin", "Library/Application Support"),
    ("linux", ".local/share"),
    ("freebsd14", ".local/share"),
])
def test_platform_defaults(monkeypatch, tmp_path, platform, base):
    monkeypatch.setattr(app_paths, "_windows_local_data", lambda home: home / "AppData/Local")
    assert app_paths.app_data_dir(platform=platform, environ={}, home=tmp_path) == tmp_path / base / "novelDownloader"


@pytest.mark.parametrize("platform,variable", [("win32", "LOCALAPPDATA"), ("linux", "XDG_DATA_HOME")])
def test_platform_environment_and_invalid_relative_paths(monkeypatch, tmp_path, platform, variable):
    monkeypatch.setattr(app_paths, "_windows_local_data", lambda home: home / "AppData/Local")
    expected = tmp_path / "redirected" / "novelDownloader"
    assert app_paths.app_data_dir(platform=platform, environ={variable: str(tmp_path / "redirected")}, home=tmp_path) == expected
    for invalid in ("", "relative/path"):
        assert app_paths.app_data_dir(platform=platform, environ={variable: invalid}, home=tmp_path) == app_paths.app_data_dir(platform=platform, environ={}, home=tmp_path)


def test_override_requires_absolute_path(tmp_path):
    assert app_paths.app_data_dir(environ={app_paths.DATA_DIR_ENV: str(tmp_path)}) == tmp_path
    with pytest.raises(app_paths.ApplicationDataError, match="absolute"):
        app_paths.app_data_dir(environ={app_paths.DATA_DIR_ENV: "relative"})


@pytest.mark.parametrize("frozen", [False, True])
def test_source_and_frozen_select_same_data_regardless_of_cwd(monkeypatch, tmp_path, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "installation" / "app.exe"))
    monkeypatch.chdir(tmp_path)
    path = app_paths.app_data_dir(platform="linux", environ={}, home=tmp_path / "home")
    assert path == tmp_path / "home/.local/share/novelDownloader"
    assert not path.exists()  # Selecting a path has no filesystem side effects.


def test_source_legacy_root_is_module_directory_not_cwd(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    assert app_paths.legacy_app_dir() == Path(app_paths.__file__).resolve().parent


@pytest.mark.parametrize("location", ["installation/data", "bundle/data"])
def test_frozen_rejects_writes_inside_installation_or_bundle(monkeypatch, tmp_path, location):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "installation/app.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "bundle"), raising=False)
    monkeypatch.setenv(app_paths.DATA_DIR_ENV, str(tmp_path / location))
    with pytest.raises(app_paths.ApplicationDataError, match="installation/bundle"):
        app_paths.prepare_app_data()
    assert not (tmp_path / location).exists()


def test_unwritable_target_does_not_fall_back_to_exe(monkeypatch, tmp_path):
    target = tmp_path / "occupied"
    target.write_text("file, not directory", encoding="utf-8")
    monkeypatch.setenv(app_paths.DATA_DIR_ENV, str(target))
    with pytest.raises(app_paths.ApplicationDataError, match="Cannot initialize"):
        app_paths.prepare_app_data()
    assert target.read_text(encoding="utf-8") == "file, not directory"


@pytest.mark.parametrize("frozen", [False, True])
def test_default_profile_imports_legacy_data_before_use(monkeypatch, tmp_path, frozen):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "queue.json").write_text("[]", encoding="utf-8")
    monkeypatch.delenv(app_paths.DATA_DIR_ENV)
    monkeypatch.setattr(app_paths, "app_data_dir", lambda: new)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", str(old / "app.exe"))
    monkeypatch.setattr(app_paths, "__file__", str(old / "app_paths.py"))
    assert app_paths.prepare_app_data() == new
    assert (new / "queue.json").read_bytes() == b"[]"
    assert (old / "queue.json").read_bytes() == b"[]"


def test_all_consumers_use_same_profile(monkeypatch, tmp_path):
    from adapter_tools import user_adapter_dir
    from downloader_task import cache_root
    from textfilter import ensure_rules_file
    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
    from main_window import NovelDownloaderUI

    profile = tmp_path / "profile"
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    app = QApplication.instance() or QApplication([])
    window = NovelDownloaderUI()
    try:
        assert cache_root() == profile / "cache"
        assert user_adapter_dir() == profile / "user_adapters"
        assert ensure_rules_file() == profile / "filter_rules.txt"
        for attr, filename in [
            ("queue_file", "queue.json"),
            ("preferences_file", "preferences.json"),
            ("site_settings_file", "site_settings.json"),
            ("history_file", "history.json"),
        ]:
            assert getattr(window, attr) == profile / filename
        window.url_input.setText("https://example.test/book")
        window.add_btn.click()
        assert window.queue_writer.flush(timeout=2)
        assert json.loads((profile / "queue.json").read_text())[0]["url"] == "https://example.test/book"
    finally:
        window.close()


def test_log_handler_uses_writable_profile(monkeypatch, tmp_path):
    import logging
    import app_logging

    handlers = []
    monkeypatch.setattr(app_logging.logging, "basicConfig", lambda **kwargs: handlers.extend(kwargs["handlers"]))
    app_logging.configure_logging()
    try:
        handlers[0].emit(logging.makeLogRecord({"msg": "writable profile"}))
    finally:
        handlers[0].close()
    assert "writable profile" in (tmp_path / "profile/novelDownloader.log").read_text()


def test_gui_initialization_failure_returns_error_instead_of_empty_window(monkeypatch):
    import gui_launcher
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    messages = []
    monkeypatch.setattr(gui_launcher, "QApplication", lambda _argv: app)
    monkeypatch.setattr(gui_launcher.QMessageBox, "critical", lambda *args: messages.append(args[-1]))
    monkeypatch.setattr(gui_launcher, "configure_logging", lambda: None)

    def fail():
        raise app_paths.ApplicationDataError("migration blocked")

    monkeypatch.setattr(gui_launcher, "prepare_app_data", fail)
    assert gui_launcher.main() == 1
    assert len(messages) == 1 and "migration blocked" in messages[0]


def test_gui_launcher_shows_and_activates_main_window_before_event_loop(monkeypatch):
    import gui_launcher
    from PyQt6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon

    real_app = QApplication.instance() or QApplication([])
    visible = []

    class AppProxy:
        def __init__(self, _argv):
            pass

        def processEvents(self):
            real_app.processEvents()

        def exec(self):
            real_app.processEvents()
            visible.extend(window for window in real_app.topLevelWidgets()
                           if isinstance(window, QMainWindow) and window.isVisible())
            return 0

    monkeypatch.setattr(gui_launcher, "prepare_app_data", lambda: None)
    monkeypatch.setattr(gui_launcher, "configure_logging", lambda: None)
    monkeypatch.setattr(gui_launcher, "migration_notice", lambda: "")
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    monkeypatch.setattr(gui_launcher, "QApplication", AppProxy)

    try:
        assert gui_launcher.main() == 0
        assert len(visible) == 1
        assert visible[0].windowTitle().startswith("小說下載器")
    finally:
        for window in visible:
            window.close()


def test_conflict_notice_identifies_preserved_legacy_data(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "queue.json").write_text("[]", encoding="utf-8")
    (new / "queue.json").write_text("[]", encoding="utf-8")
    monkeypatch.delenv(app_paths.DATA_DIR_ENV)
    monkeypatch.setattr(app_paths, "app_data_dir", lambda: new)
    monkeypatch.setattr(app_paths, "legacy_app_dir", lambda: old)
    assert str(old) in app_paths.migration_notice()
    assert str(new / app_paths.MIGRATION_FILE) in app_paths.migration_notice()
