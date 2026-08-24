"""Small crash-safe helpers for local JSON state files."""
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_path_locks = {}
_path_locks_guard = threading.Lock()


def _lock_for(path):
    """Return one process-local lock for each absolute state-file path."""
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.Lock())


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        if Path(path).exists():
            logger.warning("Cannot read JSON state %s: %s", path, exc)
        return default


def _replace_with_retry(part, path, attempts=10):
    """Replace a state file, tolerating short Windows/OneDrive file locks."""
    for attempt in range(attempts):
        try:
            os.replace(part, path)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(0.1 * (2 ** attempt), 1.0))


def write_json(path, value):
    path = Path(path)
    with _lock_for(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # A fixed ``name.part`` collides with another writer and is easily held by
        # OneDrive while it is syncing.  A unique sibling avoids both problems.
        fd, part_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".part", dir=path.parent)
        os.close(fd)
        part = Path(part_name)
        try:
            part.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            _replace_with_retry(part, path)
        finally:
            try:
                part.unlink()
            except FileNotFoundError:
                pass


class LatestJsonWriter:
    """Write JSON off the caller thread while coalescing stale pending values."""

    def __init__(self, write_func=None):
        self._write_func = write_func or write_json
        self._condition = threading.Condition()
        self._pending = {}
        self._busy = False
        self._closed = False
        self._worker = threading.Thread(
            target=self._run, name="novelDownloader-json-writer", daemon=True
        )
        self._worker.start()

    def submit(self, path, value):
        path = Path(path)
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        with self._condition:
            if self._closed:
                return False
            self._pending[key] = (path, value)
            self._condition.notify()
        return True

    def _run(self):
        while True:
            with self._condition:
                while not self._pending and not self._closed:
                    self._condition.wait()
                if self._closed and not self._pending:
                    return
                _key, (path, value) = self._pending.popitem()
                self._busy = True
            try:
                self._write_func(path, value)
            except OSError as exc:
                logger.warning("Cannot save JSON state %s: %s", path, exc)
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()

    def flush(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self._pending or self._busy:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def close(self, timeout=2):
        self.flush(timeout=timeout)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._worker.join(timeout=timeout)
