import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import app_paths


def put(root, relative, content):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_migrates_all_data_and_keeps_original_bytes(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    files = {
        "queue.json": '[{"url":"https://example.test/book"}]',
        "preferences.json": '{"selected_dir":"D:/Books"}',
        "site_settings.json": '{"example.test":{"delay":3}}',
        "history.json": '[{"title":"old"}]',
        "cache/book/0001.txt": "chapter one",
        "cache/book/progress.json": '{"completed_count":1}',
        "user_adapters/custom.py": "raise RuntimeError('must stay disabled')",
        "user_adapters/custom.py.disabled": "disabled",
        "filter_rules.txt": "ad text",
        "filter_rules_example_test.txt": "site ad",
        "novelDownloader.log": "previous log",
        "novelDownloader.log.1": "older log",
    }
    for relative, content in files.items():
        put(old, relative, content)
    original = snapshot(old)
    report = app_paths.migrate_legacy_data(old, new)
    assert report["complete"]
    assert all((new / name).read_bytes() == content for name, content in original.items())
    assert snapshot(old) == original
    assert set(report["files"].values()) == {"copied"}


def test_missing_legacy_file_completes_migration(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    report = app_paths.migrate_legacy_data(old, new)
    assert report["complete"]
    assert json.loads((new / app_paths.MIGRATION_FILE).read_text())["complete"]


def test_missing_legacy_directories_complete_migration(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "queue.json").write_text("[]", encoding="utf-8")
    report = app_paths.migrate_legacy_data(old, new)
    assert report["complete"]
    assert (new / "queue.json").read_text() == "[]"
    assert not (new / "cache").exists()
    assert not (new / "user_adapters").exists()


def test_unreadable_legacy_file_stops_without_complete_journal(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    queue = put(old, "queue.json", "[]")
    real_stat = app_paths.Path.stat

    def blocked_stat(path, *args, **kwargs):
        if path == queue:
            raise PermissionError("legacy queue is locked")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(app_paths.Path, "stat", blocked_stat)
    with pytest.raises(app_paths.ApplicationDataError, match="Cannot (inspect|migrate) legacy"):
        app_paths.migrate_legacy_data(old, new)
    journal = json.loads((new / app_paths.MIGRATION_FILE).read_text())
    assert journal["complete"] is False
    assert not (new / "queue.json").exists()


def test_unreadable_legacy_directory_stops_without_complete_journal(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    cache = old / "cache"
    cache.mkdir(parents=True)
    put(old, "queue.json", "[]")
    real_iterdir = app_paths.Path.iterdir

    def blocked_iterdir(path, *args, **kwargs):
        if path == cache:
            raise OSError("legacy cache directory is unavailable")
        return real_iterdir(path, *args, **kwargs)

    monkeypatch.setattr(app_paths.Path, "iterdir", blocked_iterdir)
    with pytest.raises(app_paths.ApplicationDataError, match="Cannot read legacy application data directory"):
        app_paths.migrate_legacy_data(old, new)
    journal = json.loads((new / app_paths.MIGRATION_FILE).read_text())
    assert journal["complete"] is False
    assert not (new / "cache").exists()


def test_failed_inspection_is_retried_on_next_migration(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    queue = put(old, "queue.json", "[]")
    real_stat = app_paths.Path.stat
    blocked = True

    def sometimes_blocked(path, *args, **kwargs):
        nonlocal blocked
        if blocked and path == queue:
            raise PermissionError("legacy queue is locked")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(app_paths.Path, "stat", sometimes_blocked)
    with pytest.raises(app_paths.ApplicationDataError):
        app_paths.migrate_legacy_data(old, new)
    assert json.loads((new / app_paths.MIGRATION_FILE).read_text())["complete"] is False
    blocked = False
    report = app_paths.migrate_legacy_data(old, new)
    assert report["complete"] is True
    assert (new / "queue.json").read_text() == "[]"


def test_conflicts_keep_new_data_and_copy_missing_cache_files(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "queue.json", '[{"title":"old"}]')
    put(new, "queue.json", "[]")
    put(old, "cache/book/0001.txt", "old")
    put(new, "cache/book/0001.txt", "new")
    put(old, "cache/book/0002.txt", "second chapter")
    put(old, "cache/book/progress.json", '{"completed_count":2}')
    put(new, "cache/book/progress.json", '{"completed_count":1}')
    report = app_paths.migrate_legacy_data(old, new)
    assert (new / "queue.json").read_text() == "[]"
    assert (new / "cache/book/0001.txt").read_text() == "new"
    assert (new / "cache/book/0002.txt").read_text() == "second chapter"
    assert json.loads((new / "cache/book/progress.json").read_text())["completed_count"] == 1
    assert report["files"]["queue.json"] == "kept_existing"


def test_repeat_migration_does_not_resurrect_deleted_data(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "queue.json", "[]")
    put(old, "cache/book/0001.txt", "old")
    app_paths.migrate_legacy_data(old, new)
    (new / "cache/book/0001.txt").unlink()
    put(new, "queue.json", '[{"title":"new"}]')
    app_paths.migrate_legacy_data(old, new)
    assert not (new / "cache/book/0001.txt").exists()
    assert json.loads((new / "queue.json").read_text())[0]["title"] == "new"


def test_failed_copy_can_resume_and_never_publishes_partial_file(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "queue.json", "[]")
    original = app_paths.shutil.copyfile

    def failing_copy(source, destination):
        destination.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(app_paths.shutil, "copyfile", failing_copy)
    with pytest.raises(app_paths.ApplicationDataError, match="Cannot migrate legacy"):
        app_paths.migrate_legacy_data(old, new)
    assert not (new / "queue.json").exists()
    assert not json.loads((new / app_paths.MIGRATION_FILE).read_text())["complete"]
    assert not list(new.glob("*.part"))
    monkeypatch.setattr(app_paths.shutil, "copyfile", original)
    app_paths.migrate_legacy_data(old, new)
    assert (new / "queue.json").read_bytes() == b"[]"


@pytest.mark.parametrize("contents", ["{broken", "{}"])
def test_invalid_legacy_queue_blocks_instead_of_loading_empty_state(tmp_path, contents):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "queue.json", contents)
    with pytest.raises((ValueError, app_paths.ApplicationDataError)):
        app_paths.migrate_legacy_data(old, new)
    assert not (new / "queue.json").exists()
    assert (old / "queue.json").read_text() == contents


def test_disabled_adapter_is_not_executed_after_migration(monkeypatch, tmp_path):
    from adapter_tools import load_user_adapters

    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "user_adapters/disabled.py", "raise RuntimeError('executed disabled adapter')")
    put(old, "user_adapters/disabled.py.disabled", "disabled")
    app_paths.migrate_legacy_data(old, new)
    monkeypatch.setenv(app_paths.DATA_DIR_ENV, str(new))
    assert load_user_adapters(object) == ([], [])


def test_existing_enabled_adapter_does_not_inherit_old_disabled_marker(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "user_adapters/custom.py", "# old")
    put(old, "user_adapters/custom.py.disabled", "disabled")
    put(new, "user_adapters/custom.py", "# new")
    app_paths.migrate_legacy_data(old, new)
    assert not (new / "user_adapters/custom.py.disabled").exists()
    assert (new / "user_adapters/custom.py").read_text() == "# new"


def test_marker_is_copied_before_adapter_and_retry_keeps_it_disabled(monkeypatch, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "user_adapters/custom.py", "# adapter")
    put(old, "user_adapters/custom.py.disabled", "disabled")
    copy = app_paths._copy_verified
    seen = []

    def interrupted(source, target):
        seen.append(source.name)
        if source.suffix == ".py":
            raise PermissionError("locked")
        copy(source, target)

    monkeypatch.setattr(app_paths, "_copy_verified", interrupted)
    with pytest.raises(app_paths.ApplicationDataError, match="Cannot migrate legacy"):
        app_paths.migrate_legacy_data(old, new)
    assert seen == ["custom.py.disabled", "custom.py"]
    assert not (new / "user_adapters/custom.py").exists()
    monkeypatch.setattr(app_paths, "_copy_verified", copy)
    app_paths.migrate_legacy_data(old, new)
    assert (new / "user_adapters/custom.py.disabled").exists()


def test_copy_race_never_overwrites_new_file(monkeypatch, tmp_path):
    source = put(tmp_path / "old", "queue.json", "[]")
    target = tmp_path / "new/queue.json"
    copy = app_paths.shutil.copyfile

    def race(old, part):
        copy(old, part)
        target.write_text('[{"title":"concurrent"}]', encoding="utf-8")

    monkeypatch.setattr(app_paths.shutil, "copyfile", race)
    with pytest.raises(FileExistsError):
        app_paths._copy_verified(source, target)
    assert "concurrent" in target.read_text()


def test_symlink_source_is_not_followed(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    outside = put(tmp_path, "outside.json", "[]")
    try:
        (old / "queue.json").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires Windows Developer Mode or privileges")
    with pytest.raises(app_paths.ApplicationDataError, match="symbolic link"):
        app_paths.migrate_legacy_data(old, new)
    assert not (new / "queue.json").exists()


def test_concurrent_migrations_preserve_data(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "queue.json", "[]")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: app_paths.migrate_legacy_data(old, new), range(2)))
    assert all(report["complete"] for report in results)
    assert (new / "queue.json").read_bytes() == b"[]"


def test_copy_detects_source_changes_before_publication(monkeypatch, tmp_path):
    source = put(tmp_path / "old", "queue.json", "[]")
    target = tmp_path / "new/queue.json"
    copy = app_paths.shutil.copyfile

    def changed_during_copy(old, part):
        copy(old, part)
        old.write_text('[{"title":"changed"}]', encoding="utf-8")

    monkeypatch.setattr(app_paths.shutil, "copyfile", changed_during_copy)
    with pytest.raises(app_paths.ApplicationDataError, match="changed while copying"):
        app_paths._copy_verified(source, target)
    assert not target.exists()


def test_complete_journal_prevents_merging_another_installation(tmp_path):
    first, second, new = tmp_path / "first", tmp_path / "second", tmp_path / "new"
    put(first, "queue.json", '[{"title":"first"}]')
    put(second, "queue.json", '[{"title":"second"}]')
    put(second, "cache/other-book/0001.txt", "other install")
    app_paths.migrate_legacy_data(first, new)
    app_paths.migrate_legacy_data(second, new)
    assert "first" in (new / "queue.json").read_text()
    assert not (new / "cache/other-book/0001.txt").exists()
    assert (second / "cache/other-book/0001.txt").exists()


def test_broken_journal_does_not_restart_migration(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    put(old, "queue.json", "[]")
    put(new, app_paths.MIGRATION_FILE, "{broken")
    with pytest.raises(ValueError):
        app_paths.migrate_legacy_data(old, new)
    assert not (new / "queue.json").exists()
