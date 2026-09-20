"""Exercise an actual Windows GUI EXE in a fresh, disposable test directory.

Usage: python tools/smoke_frozen.py EXE --work-dir NEW_DIRECTORY
No website access, real user profiles, ACL changes, or production hooks needed:
a synthetic legacy user adapter schedules checks in the normal Qt event loop.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROBE = r'''
import json
import logging
import os
import sys
import traceback
from pathlib import Path
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication


def check_application():
    result = Path(os.environ["NOVELDOWNLOADER_SMOKE_RESULT"])
    app = QApplication.instance()
    exit_code = 1
    window = None
    try:
        from app_paths import prepare_app_data
        from adapter_tools import user_adapter_dir, write_generated_adapter, disable_adapter
        from downloader_task import cache_root
        from textfilter import ensure_rules_file, load_rules
        from state_io import read_json, write_json
        from main_window import USER_ADAPTER_ERRORS

        window = next(w for w in app.topLevelWidgets() if hasattr(w, "queue_file"))
        root = prepare_app_data()
        assert getattr(sys, "frozen", False)
        assert root == Path(os.environ["LOCALAPPDATA"]) / "novelDownloader"
        assert not root.is_relative_to(Path(sys.executable).parent)
        assert not root.is_relative_to(Path(sys._MEIPASS))
        assert window.queue_file == root / "queue.json"
        assert window.preferences_file == root / "preferences.json"
        assert window.site_settings_file == root / "site_settings.json"
        assert window.history_file == root / "history.json"
        assert cache_root() == root / "cache"
        assert user_adapter_dir() == root / "user_adapters"
        assert not USER_ADAPTER_ERRORS, USER_ADAPTER_ERRORS
        assert (user_adapter_dir() / "disabled.py.disabled").exists()
        assert any(job["id"] == "legacy-job" for job in window.jobs)
        assert load_rules() == [("str", "legacy advertisement")]
        if os.environ["NOVELDOWNLOADER_SMOKE_STAGE"] == "first":
            assert (cache_root() / "legacy-book/0001.txt").read_text() == "legacy chapter"
            assert window.selected_dir.name == "legacy-output"
            assert window.site_settings["example.invalid"]["delay"] == 3
            assert read_json(window.history_file, [])[0]["id"] == "old-history"
            window.url_input.setText("https://example.invalid/new-book")
            window.title_input.setText("Smoke book")
            window.add_btn.click()
            assert window.queue_writer.flush(timeout=5)
            window.selected_dir = root / "new-output"
            window.selected_dir.mkdir()
            window.save_preferences()
            window.site_settings["smoke.invalid"] = {"delay": 4}
            write_json(window.site_settings_file, window.site_settings)
            window.record_history({"id": "new-history", "url": "https://example.invalid/new-book"})
            cache = cache_root() / "new-book/0001.txt"
            cache.parent.mkdir(parents=True)
            cache.write_text("new chapter", encoding="utf-8")
            adapter = write_generated_adapter("https://smoke.invalid/book", "smoke_generated")
            disable_adapter(adapter)
            ensure_rules_file("smoke.invalid")
            # Removing only synthetic migrated data must not resurrect it later.
            (cache_root() / "legacy-book/0001.txt").unlink()
            logging.getLogger("frozen-smoke").warning("frozen-write-ok")
        else:
            assert any(job["url"] == "https://example.invalid/new-book" for job in window.jobs)
            assert window.selected_dir == root / "new-output"
            assert window.site_settings["smoke.invalid"]["delay"] == 4
            assert any(row["id"] == "new-history" for row in read_json(window.history_file, []))
            assert (cache_root() / "new-book/0001.txt").read_text() == "new chapter"
            assert not (cache_root() / "legacy-book/0001.txt").exists()
            assert (user_adapter_dir() / "smoke_generated.py.disabled").exists()
            assert (root / "filter_rules_smoke_invalid.txt").exists()
            assert "frozen-write-ok" in (root / "novelDownloader.log").read_text(encoding="utf-8")
        window.close()
        result.write_text(json.dumps({"status": "PASS", "data_dir": str(root),
                                      "frozen": True, "bundle_dir": str(sys._MEIPASS)}, indent=2), encoding="utf-8")
        exit_code = 0
    except Exception:
        result.write_text(json.dumps({"status": "FAIL", "traceback": traceback.format_exc()}, indent=2), encoding="utf-8")
        if window:
            window.close()
    finally:
        app.exit(exit_code)


QTimer.singleShot(0, check_application)
'''


def inventory(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                result[path.relative_to(root).as_posix()] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("exe", type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This validation runs the real Windows EXE and requires Windows.")
    exe = args.exe.resolve(strict=True)
    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    installation = root / "installation with spaces"
    shutil.copytree(exe.parent, installation)
    legacy_output = root / "legacy-output"
    legacy_output.mkdir()
    files = {
        "queue.json": json.dumps([{"id": "legacy-job", "url": "https://example.invalid/legacy", "title": "Legacy", "status": "stopped"}]),
        "preferences.json": json.dumps({"output_dir": str(legacy_output)}),
        "site_settings.json": json.dumps({"example.invalid": {"delay": 3}}),
        "history.json": json.dumps([{"id": "old-history", "url": "https://example.invalid/old"}]),
        "cache/legacy-book/0001.txt": "legacy chapter",
        "cache/legacy-book/progress.json": '{"completed_count":1}',
        "user_adapters/probe.py": PROBE,
        "user_adapters/disabled.py": "raise RuntimeError('Disabled adapter was executed')",
        "user_adapters/disabled.py.disabled": "disabled",
        "filter_rules.txt": "legacy advertisement",
        "novelDownloader.log": "legacy log\n",
    }
    for relative, contents in files.items():
        target = installation / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    before = inventory(installation)
    environment = os.environ.copy()
    environment.pop("NOVELDOWNLOADER_DATA_DIR", None)
    environment["LOCALAPPDATA"] = str(root / "local app data")
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    reports = []
    for stage in ("first", "second"):
        result = root / f"{stage}.json"
        environment["NOVELDOWNLOADER_SMOKE_RESULT"] = str(result)
        environment["NOVELDOWNLOADER_SMOKE_STAGE"] = stage
        # A windowed onefile child can keep PIPE handles open after its parent
        # is killed. Use files, and terminate only this owned process tree.
        stderr_path = root / f"{stage}.stderr.txt"
        with (root / f"{stage}.stdout.txt").open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen([str(installation / exe.name)], cwd=root, env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW,
                                       stdout=stdout, stderr=stderr)
            try:
                returncode = process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                               check=True, creationflags=subprocess.CREATE_NO_WINDOW,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                process.wait(timeout=10)
                raise
        if not result.exists():
            raise RuntimeError(f"{stage}: EXE exited {returncode} without a probe result: {stderr_path.read_text(errors='replace')}")
        report = json.loads(result.read_text(encoding="utf-8"))
        reports.append(report)
        if returncode or report["status"] != "PASS":
            raise RuntimeError(f"{stage}: {report}")
    after = inventory(installation)
    assert before == after, "Installation/legacy data was changed by the EXE"
    summary = {"status": "PASS", "exe": str(exe), "launches": reports,
               "installation_unchanged": True, "legacy_files_preserved": True}
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
