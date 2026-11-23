"""Engine and session helpers. SQLite with foreign keys on and a busy timeout (worker and bot share the file).

File databases use WAL journaling (docs/contract-requests/ops.md item 1): readers never block the other process's
commit, so the worker holding a read while it talks to Telegram or Upload-Post cannot stall the bot. Both processes
run in containers on the same Docker VM, so the `-shm` file is shared through one kernel (WAL is not safe across
machines or network filesystems; the host never opens the DB, see Makefile). `synchronous=NORMAL` is the
recommended pairing with WAL: a power loss can drop the last commits but never corrupts the file.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool

BUSY_TIMEOUT_MS = 15_000
WAL_RETRY_S = 15.0

log = logging.getLogger(__name__)


def _enable_wal(cur: Any, *, retry_s: float = WAL_RETRY_S) -> None:
    """Switch the file to WAL. Switching needs an exclusive lock and SQLite does not apply the busy timeout to it, so
    when the worker and the bot open a brand-new database at the same moment one of them gets "database is locked"
    (seen on the Docker bind mount, Phase 2). Retry briefly; once any process has switched, the mode is stored in the
    file and later connections only read it. If it still cannot switch, keep the default rollback journal (correct,
    just more lock waits) and log it."""
    deadline = time.monotonic() + retry_s
    while True:
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if time.monotonic() >= deadline:
                log.warning("could not switch SQLite to WAL (%s); using the rollback journal", exc)
                return
            time.sleep(0.05)


def make_engine(database_url: str, *, echo: bool = False, poolclass: type[Pool] | None = None) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"poolclass": poolclass} if poolclass else {}
    engine = create_engine(database_url, echo=echo, connect_args={"timeout": BUSY_TIMEOUT_MS / 1000}, **kwargs)

    if url.get_backend_name() == "sqlite":
        file_db = bool(url.database) and url.database != ":memory:" and not str(url.database).startswith("file::memory")

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if file_db:
                _enable_wal(cur)
                cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
