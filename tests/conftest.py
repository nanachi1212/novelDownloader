"""Isolate even collection-time adapter imports from real application data."""
import os
import tempfile

import pytest


def pytest_configure(config):
    config._app_profile = tempfile.TemporaryDirectory(prefix="noveldownloader-tests-")
    config._old_profile = os.environ.get("NOVELDOWNLOADER_DATA_DIR")
    os.environ["NOVELDOWNLOADER_DATA_DIR"] = config._app_profile.name
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_unconfigure(config):
    if config._old_profile is None:
        os.environ.pop("NOVELDOWNLOADER_DATA_DIR", None)
    else:
        os.environ["NOVELDOWNLOADER_DATA_DIR"] = config._old_profile
    config._app_profile.cleanup()


@pytest.fixture(autouse=True)
def isolated_app_data(monkeypatch, tmp_path):
    monkeypatch.setenv("NOVELDOWNLOADER_DATA_DIR", str(tmp_path / "profile"))
