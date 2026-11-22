"""The Alembic migrations produce exactly the schema the models declare."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from clipforge.db import Base, CampaignRow, make_engine, make_session_factory, session_scope
from clipforge.db.backup import backup_db
from clipforge.db.migrate import upgrade
from tests.support.factories import campaign_in


def test_upgrade_head_matches_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    engine = make_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_upgrade_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    upgrade(url)


def test_backup_keeps_newest(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    url = f"sqlite:///{db}"
    upgrade(url)
    engine = make_engine(url)
    with session_scope(make_session_factory(engine)) as s:
        c = campaign_in()
        data = c.model_dump(mode="json")
        data["platform_marketplace"] = data.pop("marketplace")
        s.add(CampaignRow(slug=c.slug, **data))
    engine.dispose()
    out = tmp_path / "backups"
    paths = [backup_db(db, out, keep=2, now=datetime(2026, 11, d, 4, 0, tzinfo=UTC)) for d in (20, 21, 22)]
    assert sorted(out.iterdir()) == paths[1:]
    restored = make_engine(f"sqlite:///{paths[-1]}")
    with restored.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from campaigns").scalar() == 1
    restored.dispose()
