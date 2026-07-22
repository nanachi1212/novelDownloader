import threading
import time

import main_window


def test_queue_thread_runs_multiple_books_in_parallel(monkeypatch, tmp_path):
    jobs = [
        {"url": f"https://example.com/{n}", "title": f"book-{n}",
         "start": None, "end": None, "status": "pending"}
        for n in range(3)
    ]
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_download(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    monkeypatch.setattr(main_window, "download_novel", fake_download)
    queue = main_window.QueueThread(jobs, tmp_path, delay=0, max_workers=2)

    queue.run()

    assert max_active == 2
    assert [job["status"] for job in jobs] == ["done", "done", "done"]
