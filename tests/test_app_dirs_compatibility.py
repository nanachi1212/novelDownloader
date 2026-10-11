"""The PR compatibility API must use the verified, isolated main policy."""
import json

import pytest

import app_dirs
import app_paths
import chrome_cookies


def test_compatibility_helpers_respect_isolated_profile(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    (old / "queue.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(app_paths, "legacy_app_dir", lambda: old)
    profile = tmp_path / "profile"
    assert app_dirs.get_app_data_dir() == profile
    assert app_dirs.migrate_legacy_data() == profile
    assert not (profile / "queue.json").exists()
    assert (old / "queue.json").read_bytes() == b"[]"


def test_compatibility_migration_propagates_failure_and_can_retry(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "queue.json").write_text("[]", encoding="utf-8")
    monkeypatch.delenv(app_paths.DATA_DIR_ENV)
    monkeypatch.setattr(app_paths, "legacy_app_dir", lambda: old)
    monkeypatch.setattr(app_paths, "app_data_dir", lambda: new)
    copy = app_paths.shutil.copyfile

    def fail(source, destination):
        destination.write_bytes(b"partial")
        raise PermissionError("locked")

    monkeypatch.setattr(app_paths.shutil, "copyfile", fail)
    with pytest.raises(app_paths.ApplicationDataError, match="locked"):
        app_dirs.migrate_legacy_data()
    assert (old / "queue.json").read_bytes() == b"[]"
    assert not (new / "queue.json").exists()
    assert not json.loads((new / app_paths.MIGRATION_FILE).read_text())["complete"]
    monkeypatch.setattr(app_paths.shutil, "copyfile", copy)
    assert app_dirs.migrate_legacy_data() == new
    assert (new / "queue.json").read_bytes() == b"[]"
    assert (old / "queue.json").read_bytes() == b"[]"


def test_browser_cookie_profile_uses_app_data_and_is_cleaned(tmp_path, monkeypatch):
    commands = []

    class Browser:
        def terminate(self):
            pass

        def wait(self, timeout):
            return 0

    def spawn(command):
        commands.append(command)
        return Browser()

    monkeypatch.setattr(chrome_cookies, "find_chrome", lambda: tmp_path / "chrome.exe")
    monkeypatch.setattr(chrome_cookies, "_free_port", lambda: 18400)
    monkeypatch.setattr(chrome_cookies.subprocess, "Popen", spawn)
    monkeypatch.setattr(chrome_cookies.ChromeCookieSession, "_wait_ready", lambda self: None)
    session = chrome_cookies.ChromeCookieSession()
    try:
        session.open("https://example.test")
        profile = session.profile
        assert profile.parent == tmp_path / "profile" / "browser_profiles"
        assert profile.is_dir()
        assert f"--user-data-dir={profile}" in commands[0]
    finally:
        session.close()
    assert not profile.exists()
