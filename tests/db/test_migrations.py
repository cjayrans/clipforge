"""The Alembic migrations produce exactly the schema the models declare."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

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


def test_file_database_uses_wal(tmp_path: Path) -> None:
    """docs/contract-requests/ops.md item 1: worker and bot share one SQLite file in WAL mode."""
    engine = make_engine(f"sqlite:///{tmp_path / 'wal.db'}")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            assert conn.execute(text("PRAGMA synchronous")).scalar() == 1  # NORMAL
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    finally:
        engine.dispose()


def test_memory_database_keeps_default_journal() -> None:
    engine = make_engine("sqlite:///:memory:")
    try:
        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA journal_mode")).scalar() == "memory"
    finally:
        engine.dispose()


def test_backup_of_wal_database_is_self_contained(tmp_path: Path) -> None:
    db = tmp_path / "live.db"
    engine = make_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (x INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    out = backup_db(db, tmp_path / "backups", keep=2)
    engine.dispose()
    con = sqlite3.connect(out)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        con.close()


def test_wal_switch_retries_while_another_process_holds_the_lock() -> None:
    """Phase 2 finding on the Docker bind mount: two processes opening a new DB at once -> 'database is locked'."""
    from clipforge.db import session as db_session

    class Cur:
        def __init__(self, failures: int) -> None:
            self.failures = failures
            self.calls = 0

        def execute(self, sql: str) -> None:
            self.calls += 1
            if self.failures:
                self.failures -= 1
                raise sqlite3.OperationalError("database is locked")

    cur = Cur(failures=3)
    db_session._enable_wal(cur, retry_s=5)
    assert cur.calls == 4
    stuck = Cur(failures=10_000)
    db_session._enable_wal(stuck, retry_s=0.1)  # gives up quietly: the rollback journal still works
    assert stuck.calls >= 1


def test_wal_switch_reraises_other_errors() -> None:
    from clipforge.db import session as db_session

    class Cur:
        def execute(self, sql: str) -> None:
            raise sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db_session._enable_wal(Cur())


def test_0002_upgrades_a_database_with_rows(tmp_path: Path) -> None:
    """The live database already has campaigns: 0002 adds the new columns with safe defaults and keeps the rows."""
    db = tmp_path / "old.db"
    url = f"sqlite:///{db}"
    upgrade(url, "0001")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO campaigns (slug, platform_marketplace, name, url, submission_url, cpm_usd, min_payout_usd,"
        " min_views_per_post, allowed_platforms, required_tags, disclosure_text, rules_notes, is_political, priority,"
        " source_urls, content_kind, footage_mode, allow_stream_download, cpm_by_platform, min_payout_by_platform,"
        " unpaid_viewer_countries, status, created_at, updated_at)"
        " VALUES ('old', 'content_rewards', 'Old', 'https://e/a', 'https://e/a', 1.0, 0, 0, '[\"tiktok\"]', '[]',"
        " '#ad', '', 0, 0, '[]', 'talk', 'supplied', 0, '{}', '{}', '[]',"
        " 'active', '2026-11-22 00:00:00', '2026-11-22 00:00:00')"
    )
    con.commit()
    con.close()
    upgrade(url)
    engine = make_engine(url)
    with session_scope(make_session_factory(engine)) as s:
        from clipforge.ingest.models import campaign_from_row

        row = s.query(CampaignRow).one()
        c = campaign_from_row(row)
        assert c.name == "Old"
        assert c.overlay is None
        assert c.copy_style_notes == ""
        assert c.stream_channels == []
        assert c.max_source_minutes is None
    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(candidates)")}
        assert "hook_history" in cols
    engine.dispose()


def test_0003_adds_moment_columns_and_leaves_approved_clips_alone(tmp_path: Path) -> None:
    """Moment selection only adds nullable columns: an approved clip and its candidate keep every value."""
    db = tmp_path / "old.db"
    url = f"sqlite:///{db}"
    upgrade(url, "0002")
    ts = "'2026-11-22 00:00:00'"
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO campaigns (slug, platform_marketplace, name, url, submission_url, cpm_usd, min_payout_usd,"
        " min_views_per_post, allowed_platforms, required_tags, disclosure_text, rules_notes, is_political, priority,"
        " source_urls, content_kind, footage_mode, allow_stream_download, cpm_by_platform, min_payout_by_platform,"
        " unpaid_viewer_countries, copy_style_notes, stream_channels, status, created_at, updated_at)"
        " VALUES ('old', 'content_rewards', 'Old', 'https://e/a', 'https://e/a', 1.0, 0, 0, '[\"tiktok\"]', '[]',"
        f" '#ad', '', 0, 0, '[]', 'game', 'supplied', 0, '{{}}', '{{}}', '[]', '', '[]', 'active', {ts}, {ts})"
    )
    con.execute(
        f"INSERT INTO sources (campaign_id, url, status, created_at) VALUES (1, 'https://e/v.mp4', 'ranked', {ts})"
    )
    con.execute(
        "INSERT INTO candidates (source_id, start_s, end_s, score, standalone, brief_ok, reasons, prompt_version,"
        f" state, hook, hook_history, created_at) VALUES (1, 10, 40, 77, 1, 1, 'old', 'score_windows@1', 'rendered',"
        f" 'old hook', '[]', {ts})"
    )
    con.execute(
        "INSERT INTO clips (candidate_id, account_set, style_preset, path, duration_s, sha256, state, rendered_at,"
        f" created_at) VALUES (1, 'gaming-1', 'bold-yellow', '/x.mp4', 30, 'abc', 'approved', {ts}, {ts})"
    )
    con.commit()
    before = {t: con.execute(f"SELECT * FROM {t}").fetchall() for t in ("candidates", "clips")}  # noqa: S608
    con.close()

    upgrade(url)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cand = con.execute("SELECT * FROM candidates").fetchone()
    clip = con.execute("SELECT * FROM clips").fetchone()
    assert clip["state"] == "approved"
    assert cand["score"] == 77
    assert cand["hook"] == "old hook"
    for col in (
        "archetype",
        "weights_version",
        "moment_profile",
        "score_reaction",
        "gate_results_json",
        "review_flags",
    ):
        assert cand[col] is None, col
    assert con.execute("SELECT moment_profile FROM campaigns").fetchone()[0] is None
    post_cols = {r[1] for r in con.execute("PRAGMA table_info(posts)")}
    assert {"views_24h", "views_72h"} <= post_cols
    after = {t: [tuple(r)[: len(before[t][0])] for r in con.execute(f"SELECT * FROM {t}")] for t in before}  # noqa: S608
    con.close()
    assert after == before  # every old column keeps its value
