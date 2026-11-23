"""Online SQLite backup (PRD F13): `sqlite3` backup API, keep the newest N copies."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "clipforge-"


def backup_db(db_path: Path, backups_dir: Path, keep: int, *, now: datetime | None = None) -> Path:
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    if keep < 1:
        raise ValueError("keep must be >= 1")
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    target = backups_dir / f"{PREFIX}{stamp}.db"
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            # The live DB runs in WAL mode (db/session.py); a backup should be one self-contained file.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    existing = sorted(backups_dir.glob(f"{PREFIX}*.db"))
    for old in existing[:-keep]:
        old.unlink()
    return target
