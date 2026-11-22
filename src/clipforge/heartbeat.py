"""Heartbeat files (PRD F14): each long-running process touches `data/heartbeat/<name>` every minute.

The Docker HEALTHCHECK and the bot's evening alert read the file's age.
"""

from __future__ import annotations

import time
from pathlib import Path

STALE_AFTER_S = 180.0
ALERT_AFTER_S = 600.0  # PRD F14: alert when the worker heartbeat is older than 10 min during 19:30-21:00


def beat(heartbeat_dir: Path, name: str) -> Path:
    heartbeat_dir.mkdir(parents=True, exist_ok=True)
    path = heartbeat_dir / name
    path.write_text(f"{time.time():.0f}\n", encoding="ascii")
    return path


def age_s(heartbeat_dir: Path, name: str, *, now: float | None = None) -> float | None:
    """Seconds since the last beat, or None if there has never been one."""
    path = heartbeat_dir / name
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return max(0.0, (now if now is not None else time.time()) - mtime)


def is_healthy(heartbeat_dir: Path, name: str, *, max_age_s: float = STALE_AFTER_S) -> bool:
    age = age_s(heartbeat_dir, name)
    return age is not None and age <= max_age_s
