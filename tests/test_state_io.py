import time

from state_io import read_json, write_json


def test_json_state_is_replaced_without_leftover_part(tmp_path):
    path = tmp_path / "queue.json"
    write_json(path, {"status": "pending"})
    write_json(path, {"status": "done"})

    assert read_json(path, {}) == {"status": "done"}
    assert not (tmp_path / "queue.json.part").exists()


def test_invalid_json_returns_default(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    assert read_json(path, []) == []


def test_json_state_uses_unique_temp_files_and_retries_one_drive_lock(monkeypatch, tmp_path):
    import state_io

    real_replace = state_io.os.replace
    calls = 0

    def flaky_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError(5, "Access is denied")
        return real_replace(source, target)

    monkeypatch.setattr(state_io.os, "replace", flaky_replace)
    state_io.write_json(tmp_path / "progress.json", {"completed_count": 1})

    assert state_io.read_json(tmp_path / "progress.json", {}) == {"completed_count": 1}
    assert calls == 2
    assert not list(tmp_path.glob("progress.json.*.part"))


def test_concurrent_writes_to_same_state_file_are_serialized(monkeypatch, tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    import state_io

    real_replace = state_io.os.replace
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def observed_replace(source, target):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.005)
            return real_replace(source, target)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(state_io.os, "replace", observed_replace)
    path = tmp_path / "progress.json"
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda value: state_io.write_json(path, {"value": value}), range(40)))

    assert max_active == 1
    assert "value" in state_io.read_json(path, {})
    assert not list(tmp_path.glob("progress.json.*.part"))


def test_latest_json_writer_never_blocks_caller_and_keeps_latest_value(tmp_path):
    import threading

    from state_io import LatestJsonWriter

    started = threading.Event()
    release = threading.Event()
    written = []

    def slow_write(path, value):
        started.set()
        assert release.wait(2)
        written.append((path, value))

    writer = LatestJsonWriter(write_func=slow_write)
    path = tmp_path / "queue.json"
    began = time.monotonic()
    writer.submit(path, {"status": "running"})
    elapsed = time.monotonic() - began
    assert elapsed < 0.05
    assert started.wait(1)

    writer.submit(path, {"status": "done"})
    release.set()
    assert writer.flush(timeout=2)
    writer.close()

    assert written[-1] == (path, {"status": "done"})
