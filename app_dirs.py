import os
import sys
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def get_app_data_dir() -> Path:
    """Return the application data directory based on the platform."""
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "novelDownloader"
        return Path.home() / "AppData" / "Local" / "novelDownloader"
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "novelDownloader"
    else:
        # Linux and others
        config_home = os.environ.get("XDG_DATA_HOME")
        if config_home:
            return Path(config_home) / "novelDownloader"
        return Path.home() / ".local" / "share" / "novelDownloader"

def migrate_legacy_data():
    """Safely migrate legacy data files and directories to the new app data dir.
    This copies data if it doesn't already exist in the target to avoid data loss.
    """
    if getattr(sys, "frozen", False):
        legacy_dir = Path(sys.executable).parent
    else:
        legacy_dir = Path(__file__).parent

    app_data_dir = get_app_data_dir()

    # If legacy dir and app data dir are the same (which shouldn't happen, but just in case)
    if legacy_dir.resolve() == app_data_dir.resolve():
        return

    try:
        app_data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create app data directory: {e}")
        return

    # Files to migrate
    files_to_migrate = [
        "queue.json",
        "preferences.json",
        "site_settings.json",
        "history.json",
    ]

    for filename in files_to_migrate:
        src = legacy_dir / filename
        dst = app_data_dir / filename
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                logger.info(f"Migrated {filename} to {app_data_dir}")
            except Exception as e:
                logger.error(f"Failed to migrate {filename}: {e}")

    # Directories to migrate
    dirs_to_migrate = [
        "cache",
        "user_adapters",
    ]

    for dirname in dirs_to_migrate:
        src_dir = legacy_dir / dirname
        dst_dir = app_data_dir / dirname
        if src_dir.exists() and src_dir.is_dir() and not dst_dir.exists():
            try:
                shutil.copytree(src_dir, dst_dir)
                logger.info(f"Migrated directory {dirname} to {app_data_dir}")
            except Exception as e:
                logger.error(f"Failed to migrate directory {dirname}: {e}")
