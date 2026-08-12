"""Small crash-safe helpers for local JSON state files."""
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        if Path(path).exists():
            logger.warning("Cannot read JSON state %s: %s", path, exc)
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(part, path)
