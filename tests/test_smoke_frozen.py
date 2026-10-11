"""The frozen-EXE smoke probe must end the real event loop, not only drain events.

gui_launcher.main() calls app.processEvents() before app.exec().  A probe that
ran (and called app.exit()) inside that first drain left app.exec() waiting
forever, which the smoke tool reported as a timeout.
"""
import sys
from pathlib import Path

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QWidget

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import smoke_frozen  # noqa: E402

WATCHDOG_EXIT = 99


def run_launcher_sequence(monkeypatch, tmp_path):
    """Mimic gui_launcher.main(): show window, processEvents(), then app.exec()."""
    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("NOVELDOWNLOADER_SMOKE_RESULT", str(tmp_path / "result.json"))
    monkeypatch.setenv("NOVELDOWNLOADER_SMOKE_STAGE", "first")
    window = QWidget()
    window.queue_file = tmp_path / "queue.json"  # lets the probe find its window
    window.show()
    # What loading tools/smoke_frozen.PROBE as a user adapter does at import time.
    exec(compile(smoke_frozen.PROBE, "<probe>", "exec"), {"__name__": "probe"})
    app.processEvents()
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: app.exit(WATCHDOG_EXIT))
    watchdog.start(5000)
    code = app.exec()
    watchdog.stop()
    window.close()
    return code


def test_probe_exits_the_event_loop_started_after_process_events(monkeypatch, tmp_path):
    code = run_launcher_sequence(monkeypatch, tmp_path)
    assert code != WATCHDOG_EXIT, "probe exited too early; app.exec() only ended via the watchdog"
    # The probe cannot pass outside a frozen EXE, but it must still report and exit.
    assert (tmp_path / "result.json").exists()
