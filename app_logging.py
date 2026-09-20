import logging
from logging.handlers import RotatingFileHandler
from app_paths import prepare_app_data


def configure_logging():
    root = prepare_app_data()
    handler = RotatingFileHandler(root / "novelDownloader.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
