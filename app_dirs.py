"""Compatibility helpers backed by the shared user-data and migration policy."""
from pathlib import Path

import app_paths


def get_app_data_dir() -> Path:
    """Select the user data root, including an explicit isolated profile."""
    return app_paths.app_data_dir()


def migrate_legacy_data():
    """Prepare user data with verified copies, preserving all original files.

    Failures propagate instead of allowing callers to load partial settings.
    An explicit profile stays isolated from legacy installation data.
    """
    return app_paths.prepare_app_data()
