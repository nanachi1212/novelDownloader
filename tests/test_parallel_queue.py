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


def test_queue_thread_limits_parallel_books_per_site(monkeypatch, tmp_path):
    jobs = [
        {"url": "https://a.example.com/1", "title": "a-1",
         "start": None, "end": None, "status": "pending", "site_key": "site-a"},
        {"url": "https://a.example.com/2", "title": "a-2",
         "start": None, "end": None, "status": "pending", "site_key": "site-a"},
        {"url": "https://b.example.com/1", "title": "b-1",
         "start": None, "end": None, "status": "pending", "site_key": "site-b"},
    ]
    lock = threading.Lock()
    active_by_site = {}
    max_active_total = 0
    max_active_site_a = 0

    def fake_download(url, *args, **kwargs):
        nonlocal max_active_total, max_active_site_a
        site = next(job["site_key"] for job in jobs if job["url"] == url)
        with lock:
            active_by_site[site] = active_by_site.get(site, 0) + 1
            max_active_total = max(max_active_total, sum(active_by_site.values()))
            max_active_site_a = max(max_active_site_a, active_by_site.get("site-a", 0))
        time.sleep(0.05)
        with lock:
            active_by_site[site] -= 1

    monkeypatch.setattr(main_window, "download_novel", fake_download)
    queue = main_window.QueueThread(
        jobs, tmp_path, delay=0, max_workers=3, max_workers_per_site=1
    )

    queue.run()

    assert max_active_total == 2
    assert max_active_site_a == 1
    assert [job["status"] for job in jobs] == ["done", "done", "done"]
