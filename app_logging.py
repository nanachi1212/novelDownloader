import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging():
    root = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    handler = RotatingFileHandler(root / "novelDownloader.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
