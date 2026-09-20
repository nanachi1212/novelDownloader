"""User-writable application paths and non-destructive, one-time migration.

Code/resources remain in the installation; mutable data belongs to the user.
An explicit NOVELDOWNLOADER_DATA_DIR selects an independent profile (no import
from the installation). This also isolates developers and tests from real data.
"""
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from state_io import write_json

APP_NAME = "novelDownloader"
DATA_DIR_ENV = "NOVELDOWNLOADER_DATA_DIR"
MIGRATION_FILE = ".migration-v1.json"
STATE_FILES = {
    "queue.json": list,
    "preferences.json": dict,
    "site_settings.json": dict,
    "history.json": list,
}
_prepared = set()
_notices = {}
_prepare_lock = threading.RLock()
logger = logging.getLogger(__name__)


class ApplicationDataError(RuntimeError):
    """Initialization failed; do not continue with empty or partial user data."""


def _windows_local_data(home):
    # Query Windows rather than guessing a redirected user's profile location.
    if sys.platform == "win32":
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 28, None, 0, buffer) == 0:
            return Path(buffer.value)
    return home / "AppData" / "Local"


def app_data_dir(*, platform=None, environ=None, home=None) -> Path:
    """Select a path without creating files; source and frozen use this policy."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    override = environ.get(DATA_DIR_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ApplicationDataError(f"{DATA_DIR_ENV} must be an absolute path: {path}")
        return path
    if platform == "win32":
        local = environ.get("LOCALAPPDATA", "")
        base = Path(local) if local and Path(local).is_absolute() else _windows_local_data(home)
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        xdg = environ.get("XDG_DATA_HOME", "")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".local" / "share"
    return base / APP_NAME


def legacy_app_dir() -> Path:
    """Only used as a read-only migration source, never as a write fallback."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _reject_link(path):
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ApplicationDataError(f"Migration will not follow a symbolic link/junction: {path}")


@contextmanager
def _migration_lock(root, timeout=10):
    """OS locks are released on process exit, including interrupted migration."""
    lock_path = root / ".migration.lock"
    _reject_link(lock_path)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ApplicationDataError(f"Application data is busy: {root}. Close other instances and retry.") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _legacy_files(source):
    """Return readable legacy files; never turn inspection errors into absence."""
    try:
        source.stat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ApplicationDataError(
            f"Cannot inspect legacy application data directory: {source}: {exc}"
        ) from exc
    roots = [source / name for name in STATE_FILES]
    roots += [source / "cache", source / "user_adapters"]
    try:
        roots += sorted(
            child for child in source.iterdir()
            if child.name.startswith("filter_rules") and child.suffix.lower() == ".txt"
        )
    except OSError as exc:
        raise ApplicationDataError(
            f"Cannot inspect legacy application data directory: {source}: {exc}"
        ) from exc
    roots += [source / "novelDownloader.log"]
    roots += [source / f"novelDownloader.log.{i}" for i in range(1, 4)]
    files = []

    def visit(path):
        _reject_link(path)
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ApplicationDataError(
                f"Cannot inspect legacy application data path: {path}: {exc}"
            ) from exc
        if path.name == "__pycache__" or path.name.endswith((".pyc", ".part")):
            return
        if stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(path.iterdir())
            except OSError as exc:
                raise ApplicationDataError(
                    f"Cannot read legacy application data directory: {path}: {exc}"
                ) from exc
            for child in children:
                visit(child)
        elif stat.S_ISREG(metadata.st_mode):
            files.append(path)
        else:
            raise ApplicationDataError(f"Unsupported migration source: {path}")

    for root in roots:
        visit(root)
    # Disabled markers must be committed before any executable adapter file.
    return sorted(files, key=lambda p: (not p.name.endswith(".py.disabled"), str(p)))


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _copy_verified(source, target):
    """Publish a complete copy without replacing a concurrently created file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    fd, name = tempfile.mkstemp(prefix=".migrate-", suffix=".part", dir=target.parent)
    os.close(fd)
    part = Path(name)
    try:
        shutil.copyfile(source, part)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or _digest(source) != _digest(part):
            raise ApplicationDataError(f"Legacy data changed while copying: {source}. Close the old application and retry.")
        if os.name == "nt":
            os.rename(part, target)  # Windows refuses to overwrite an existing file.
        else:
            os.link(part, target)  # Atomic no-replace publication on POSIX.
    finally:
        part.unlink(missing_ok=True)


def migrate_legacy_data(source: Path, target: Path):
    """Copy missing data once. Retain originals and all destination conflicts.

    The journal survives failures. Successful entries are never imported again,
    including after a user intentionally clears their queue/cache/adapters.
    """
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target or target.is_relative_to(source):
        raise ApplicationDataError(f"Application data must be outside the legacy installation: {target}")
    target.mkdir(parents=True, exist_ok=True)
    with _migration_lock(target):
        marker = target / MIGRATION_FILE
        _reject_link(marker)
        if marker.exists():
            report = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(report, dict) or report.get("version") != 1 or not isinstance(report.get("files"), dict):
                raise ApplicationDataError(f"Invalid migration journal: {marker}")
            if report.get("complete"):
                return report
            if report.get("source") != str(source):
                raise ApplicationDataError(f"Finish migration from {report.get('source')} before using {source}")
        else:
            report = {"version": 1, "source": str(source), "complete": False, "files": {}}
            write_json(marker, report)
        try:
            for old in _legacy_files(source):
                relative = old.relative_to(source)
                key = relative.as_posix()
                if key in report["files"]:
                    continue
                new = target / relative
                for path in (new, *new.parents):
                    if path == target:
                        break
                    _reject_link(path)
                # Preserve an existing adapter together with its enable/disable
                # choice, even if its old installation had a different choice.
                existing_adapter = (new.name.endswith(".py.disabled")
                                    and new.with_suffix("").exists())
                if new.exists() or existing_adapter:
                    report["files"][key] = "kept_existing"
                    continue
                if key in STATE_FILES:
                    try:
                        value = json.loads(old.read_text(encoding="utf-8"))
                    except OSError as exc:
                        raise ApplicationDataError(
                            f"Cannot read legacy state file: {old}: {exc}"
                        ) from exc
                    if not isinstance(value, STATE_FILES[key]):
                        raise ApplicationDataError(f"Unexpected JSON structure in legacy state: {old}")
                _copy_verified(old, new)
                report["files"][key] = "copied"
            report["complete"] = True
        except OSError as exc:
            raise ApplicationDataError(
                f"Cannot migrate legacy application data from {source}: {exc}"
            ) from exc
        finally:
            write_json(marker, report)
        conflicts = sum(status == "kept_existing" for status in report["files"].values())
        if conflicts:
            logger.warning("Migration kept %s existing files. Originals: %s; details: %s", conflicts, source, marker)
        return report


def prepare_app_data() -> Path:
    """Initialize before loading settings/adapters; never fall back to the EXE."""
    root = app_data_dir().resolve()
    source = legacy_app_dir()
    isolated = bool(os.environ.get(DATA_DIR_ENV))
    key = (root, source, isolated)
    with _prepare_lock:
        if key in _prepared:
            return root
        try:
            if getattr(sys, "frozen", False):
                forbidden = [source]
                if hasattr(sys, "_MEIPASS"):
                    forbidden.append(Path(sys._MEIPASS).resolve())
                if any(root == p or root.is_relative_to(p) for p in forbidden):
                    raise ApplicationDataError(f"Writable data cannot be inside the installation/bundle: {root}")
            root.mkdir(parents=True, exist_ok=True)
            # Actually test writes; os.access alone is unreliable on Windows.
            with tempfile.TemporaryFile(dir=root):
                pass
            if not isolated:
                report = migrate_legacy_data(source, root)
                conflicts = sum(s == "kept_existing" for s in report["files"].values())
                if conflicts:
                    _notices[root] = (
                        f"資料遷移保留了新目錄中 {conflicts} 個既有項目；未覆蓋或合併同名資料。"
                        f"舊資料保留在 {report['source']}；明細：{root / MIGRATION_FILE}"
                    )
                if report["source"] != str(source):
                    _notices[root] = (
                        f"此使用者資料目錄已完成從 {report['source']} 的遷移。"
                        f"目前安裝目錄 {source} 的舊資料不會再次自動匯入；原件仍保留。"
                    )
        except (OSError, ValueError, ApplicationDataError) as exc:
            raise ApplicationDataError(
                f"Cannot initialize application data at {root}. Legacy data remains at {source}. "
                f"Close old instances, check permissions/free space, and retry. Details: {exc}"
            ) from exc
        _prepared.add(key)
        return root


def migration_notice():
    """Expose migration conflicts through the existing GUI/CLI log."""
    return _notices.get(prepare_app_data())
