import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QEventLoop, QTimer, pyqtSignal
from PyQt6.QtWidgets import QApplication, QProgressBar

from main_window import ProgressUpdateCoalescer


class ProgressEmitter(QObject):
    progress = pyqtSignal(int, int, int)


def test_chapter_progress_burst_is_coalesced_before_rendering():
    app = QApplication.instance() or QApplication([])
    updates = ProgressUpdateCoalescer(interval_ms=25)
    rendered = []
    updates.flushed.connect(rendered.append)

    for current in range(1, 1001):
        updates.submit(0, current, 1000)

    assert rendered == []
    loop = QEventLoop()
    QTimer.singleShot(80, loop.quit)
    loop.exec()
    app.processEvents()

    assert rendered == [{0: (1000, 1000)}]


def test_multiple_books_keep_latest_progress_in_one_render():
    app = QApplication.instance() or QApplication([])
    updates = ProgressUpdateCoalescer(interval_ms=25)
    rendered = []
    updates.flushed.connect(rendered.append)

    updates.submit(0, 10, 2490)
    updates.submit(1, 6, 416)
    updates.submit(0, 11, 2490)
    loop = QEventLoop()
    QTimer.singleShot(80, loop.quit)
    loop.exec()
    app.processEvents()

    assert rendered == [{0: (11, 2490), 1: (6, 416)}]


def test_progress_storm_keeps_gui_event_loop_responsive():
    app = QApplication.instance() or QApplication([])
    updates = ProgressUpdateCoalescer(interval_ms=50)
    emitter = ProgressEmitter()
    bar = QProgressBar()
    renders = 0
    heartbeats = 0

    def render(snapshot):
        nonlocal renders
        renders += 1
        current, total = snapshot[0]
        bar.setRange(0, total)
        bar.setValue(current)

    def heartbeat():
        nonlocal heartbeats
        heartbeats += 1

    updates.flushed.connect(render)
    emitter.progress.connect(updates.submit)
    timer = QTimer()
    timer.timeout.connect(heartbeat)
    timer.start(10)

    def emit_progress():
        for current in range(1, 5001):
            emitter.progress.emit(0, current, 5000)

    worker = threading.Thread(target=emit_progress)
    worker.start()
    loop = QEventLoop()
    QTimer.singleShot(1000, loop.quit)
    loop.exec()
    worker.join()
    app.processEvents()

    assert heartbeats >= 20
    assert renders <= 20
    assert bar.value() == 5000
