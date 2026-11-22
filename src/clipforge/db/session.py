"""Engine and session helpers. SQLite with foreign keys on and a busy timeout (worker and bot share the file)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import Pool

BUSY_TIMEOUT_MS = 15_000


def make_engine(database_url: str, *, echo: bool = False, poolclass: type[Pool] | None = None) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {"poolclass": poolclass} if poolclass else {}
    engine = create_engine(database_url, echo=echo, connect_args={"timeout": BUSY_TIMEOUT_MS / 1000}, **kwargs)

    if url.get_backend_name() == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
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
