import os
import sys
import shutil
from pathlib import Path
from unittest import mock

import app_dirs


def test_get_app_data_dir_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "environ", {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local"})

    expected = Path("C:\\Users\\test\\AppData\\Local") / "novelDownloader"
    assert app_dirs.get_app_data_dir() == expected


def test_get_app_data_dir_windows_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(Path, "home", lambda: Path("C:\\Users\\test"))

    expected = Path("C:\\Users\\test") / "AppData" / "Local" / "novelDownloader"
    assert app_dirs.get_app_data_dir() == expected


def test_get_app_data_dir_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/test"))

    expected = Path("/Users/test/Library/Application Support/novelDownloader")
    assert app_dirs.get_app_data_dir() == expected


def test_get_app_data_dir_linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(Path, "home", lambda: Path("/home/test"))

    expected = Path("/home/test/.local/share/novelDownloader")
    assert app_dirs.get_app_data_dir() == expected


def test_get_app_data_dir_linux_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "environ", {"XDG_DATA_HOME": "/home/test/.config_custom"})

    expected = Path("/home/test/.config_custom/novelDownloader")
    assert app_dirs.get_app_data_dir() == expected


def test_migrate_legacy_data_safe(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new_app_data"

    legacy_dir.mkdir()

    # Create some legacy files
    (legacy_dir / "queue.json").write_text("old_queue", encoding="utf-8")
    (legacy_dir / "history.json").write_text("old_history", encoding="utf-8")

    legacy_cache = legacy_dir / "cache"
    legacy_cache.mkdir()
    (legacy_cache / "cache1.txt").write_text("old_cache", encoding="utf-8")

    legacy_adapters = legacy_dir / "user_adapters"
    legacy_adapters.mkdir()
    (legacy_adapters / "adapter1.py").write_text("old_adapter", encoding="utf-8")

    # Mock paths
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(app_dirs, "__file__", str(legacy_dir / "app_dirs.py"))
    monkeypatch.setattr(app_dirs, "__file__", str(legacy_dir / "app_dirs.py"))
    monkeypatch.setattr(app_dirs, "get_app_data_dir", lambda: new_dir)

    # Run migration
    app_dirs.migrate_legacy_data()

    # Check that new directory has the data
    assert (new_dir / "queue.json").read_text(encoding="utf-8") == "old_queue"
    assert (new_dir / "history.json").read_text(encoding="utf-8") == "old_history"
    assert (new_dir / "cache" / "cache1.txt").read_text(encoding="utf-8") == "old_cache"
    assert (new_dir / "user_adapters" / "adapter1.py").read_text(encoding="utf-8") == "old_adapter"

    # Check that legacy data still exists (we don't delete it directly during migration)
    assert (legacy_dir / "queue.json").exists()
    assert (legacy_cache / "cache1.txt").exists()


def test_migrate_legacy_data_does_not_overwrite(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "legacy"
    new_dir = tmp_path / "new_app_data"

    legacy_dir.mkdir()
    new_dir.mkdir()

    # Create legacy file
    (legacy_dir / "queue.json").write_text("old_queue", encoding="utf-8")

    # Create new file (simulate already migrated and used)
    (new_dir / "queue.json").write_text("new_queue", encoding="utf-8")

    # Mock paths
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(app_dirs, "__file__", str(legacy_dir / "app_dirs.py"))
    monkeypatch.setattr(app_dirs, "get_app_data_dir", lambda: new_dir)

    # Run migration
    app_dirs.migrate_legacy_data()

    # Ensure the new data wasn't overwritten
    assert (new_dir / "queue.json").read_text(encoding="utf-8") == "new_queue"