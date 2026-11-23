"""D6: the job table, triggers, error isolation and every job running against a temp database."""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from clipforge import heartbeat, pipeline
from clipforge.config import Settings, load_settings
from clipforge.db import EventRow, PostRow, SubmissionRow, make_session_factory
from clipforge.db.migrate import upgrade
from clipforge.publish.dispatcher import skip_evening
from clipforge.scheduler import worker
from clipforge.scheduler.worker import (
    JobSpec,
    WorkerContext,
    build_jobs,
    guarded,
    make_scheduler,
    resolve_optional,
)
from clipforge.schemas import PostState
from tests.publish.test_dispatcher import approved_clip
from tests.review.fakes import FakeNotifier
from tests.support.factories import Factory
from tests.track.helpers import real_post, submission

NY = ZoneInfo("America/New_York")
ARM_AT = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)  # Mon 20:00 ET
FIRST_SLOT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)  # Mon 20:10 ET

EXPECTED_JOBS = {
    "nightly_pipeline",
    "purge_sources",
    "backup_db",
    "youtube_views",
    "arm_message",
    "slot_dispatcher",
    "url_poller",
    "submit_reminders",
    "weekly_report",
    "weekly_learning",
    "monthly_report",
    "heartbeat",
}


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def make_ctx(settings: Settings, engine: Engine, notifier: FakeNotifier, now: datetime = FIRST_SLOT) -> WorkerContext:
    return WorkerContext(settings=settings, factory=make_session_factory(engine), notifier=notifier, clock=lambda: now)


def fire(settings: Settings, job_id: str, after: datetime) -> datetime:
    spec = next(s for s in build_jobs(settings) if s.id == job_id)
    nxt = spec.trigger.get_next_fire_time(None, after)
    assert nxt is not None
    return nxt.astimezone(NY)


# --- the table ----------------------------------------------------------


def test_every_architecture_job_exists_once(settings: Settings) -> None:
    ids = [s.id for s in build_jobs(settings)]
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_JOBS


@pytest.mark.parametrize(
    ("job_id", "after", "expected"),
    [
        ("nightly_pipeline", datetime(2026, 11, 23, 12, 0, tzinfo=NY), datetime(2026, 11, 24, 1, 0, tzinfo=NY)),
        ("purge_sources", datetime(2026, 11, 23, 12, 0, tzinfo=NY), datetime(2026, 11, 24, 3, 30, tzinfo=NY)),
        ("backup_db", datetime(2026, 11, 23, 12, 0, tzinfo=NY), datetime(2026, 11, 24, 4, 0, tzinfo=NY)),
        ("youtube_views", datetime(2026, 11, 23, 12, 30, tzinfo=NY), datetime(2026, 11, 24, 12, 0, tzinfo=NY)),
        ("weekly_report", datetime(2026, 11, 25, 9, 0, tzinfo=NY), datetime(2026, 11, 28, 8, 0, tzinfo=NY)),
        ("weekly_learning", datetime(2026, 11, 25, 9, 0, tzinfo=NY), datetime(2026, 11, 28, 8, 15, tzinfo=NY)),
        ("weekly_learning", datetime(2026, 11, 28, 8, 1, tzinfo=NY), datetime(2026, 11, 28, 8, 15, tzinfo=NY)),
        ("weekly_report", datetime(2026, 11, 28, 8, 1, tzinfo=NY), datetime(2026, 12, 5, 8, 0, tzinfo=NY)),
    ],
)
def test_cron_jobs_fire_when_the_architecture_says(
    settings: Settings, job_id: str, after: datetime, expected: datetime
) -> None:
    assert fire(settings, job_id, after) == expected


def test_monthly_report_fires_on_the_first_sunday_only(settings: Settings) -> None:
    at = datetime(2026, 11, 9, 0, 0, tzinfo=NY)
    seen: list[date] = []
    for _ in range(8):
        at = fire(settings, "monthly_report", at + timedelta(minutes=1))
        seen.append(at.date())
        assert (at.hour, at.minute) == (8, 0)
        assert at.weekday() == 6
        assert at.day <= 7
    assert seen[:3] == [date(2026, 12, 6), date(2027, 1, 3), date(2027, 2, 7)]
    assert len({(d.year, d.month) for d in seen}) == 8  # exactly once per month


def test_daily_jobs_are_dst_safe(settings: Settings) -> None:
    # US spring-forward: Sunday 2026-03-08. 04:00 that morning is EDT (UTC-4) = 08:00 UTC; the day before it is EST.
    after_dst = fire(settings, "backup_db", datetime(2026, 3, 8, 0, 0, tzinfo=NY))
    assert after_dst.utcoffset() == timedelta(hours=-4)
    assert after_dst.astimezone(UTC) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
    before_dst = fire(settings, "backup_db", datetime(2026, 3, 7, 0, 0, tzinfo=NY))
    assert before_dst.astimezone(UTC) == datetime(2026, 3, 7, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("job_id", "seconds"),
    [("slot_dispatcher", 30), ("url_poller", 30), ("arm_message", 60), ("submit_reminders", 60), ("heartbeat", 60)],
)
def test_interval_jobs(settings: Settings, job_id: str, seconds: int) -> None:
    spec = next(s for s in build_jobs(settings) if s.id == job_id)
    t0 = datetime(2026, 11, 24, 1, 0, 0, tzinfo=UTC)
    first = spec.trigger.get_next_fire_time(None, t0)
    assert first is not None
    second = spec.trigger.get_next_fire_time(first, first)
    assert second is not None
    assert (second - first).total_seconds() == seconds


def test_make_scheduler_registers_all_jobs_with_safe_defaults(settings: Settings, engine: Engine) -> None:
    scheduler = make_scheduler(make_ctx(settings, engine, FakeNotifier()))
    jobs = {j.id: j for j in scheduler.get_jobs()}  # a stopped scheduler reports its pending jobs
    assert set(jobs) == EXPECTED_JOBS
    assert scheduler.timezone == settings.app.tz
    # No job may overlap itself or pile up missed runs (applied as scheduler defaults when it starts).
    defaults = scheduler._job_defaults
    assert defaults["coalesce"] is True
    assert defaults["max_instances"] == 1


# --- error isolation ----------------------------------------------------------


def boom(_ctx: WorkerContext) -> None:
    raise RuntimeError("kaboom")


def alerts_for(session: Session, job_id: str) -> list[EventRow]:
    return list(session.scalars(select(EventRow).where(EventRow.entity == "alert", EventRow.entity_id == job_id)))


def test_a_failing_job_never_raises_and_alerts_once_per_30_minutes(
    settings: Settings, engine: Engine, notifier: FakeNotifier, session: Session
) -> None:
    clock = {"now": FIRST_SLOT}
    ctx = WorkerContext(
        settings=settings, factory=make_session_factory(engine), notifier=notifier, clock=lambda: clock["now"]
    )
    run = guarded(ctx, JobSpec("slot_dispatcher", None, boom))  # type: ignore[arg-type]
    run()
    run()
    session.expire_all()
    (alert,) = alerts_for(session, "slot_dispatcher")
    assert "RuntimeError: kaboom" in alert.detail
    clock["now"] = FIRST_SLOT + timedelta(minutes=29)
    run()
    session.expire_all()
    assert len(alerts_for(session, "slot_dispatcher")) == 1
    clock["now"] = FIRST_SLOT + timedelta(minutes=31)
    run()
    session.expire_all()
    assert len(alerts_for(session, "slot_dispatcher")) == 2


def test_failure_alerts_never_contain_secrets(
    engine: Engine, notifier: FakeNotifier, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "super-secret-upload-key")
    settings = load_settings()

    def leaky(_ctx: WorkerContext) -> None:
        raise RuntimeError("401 for key super-secret-upload-key")

    guarded(make_ctx(settings, engine, notifier), JobSpec("url_poller", None, leaky))()  # type: ignore[arg-type]
    session.expire_all()
    (alert,) = alerts_for(session, "url_poller")
    assert "super-secret-upload-key" not in alert.detail
    assert "***" in alert.detail


def test_one_failing_job_does_not_stop_the_others(settings: Settings, engine: Engine, notifier: FakeNotifier) -> None:
    ctx = make_ctx(settings, engine, notifier)
    ran: list[str] = []
    runs = [
        guarded(ctx, JobSpec("a", None, boom)),  # type: ignore[arg-type]
        guarded(ctx, JobSpec("b", None, lambda _c: ran.append("b"))),  # type: ignore[arg-type]
    ]
    for r in runs:
        r()
    assert ran == ["b"]


# --- lazy targets ----------------------------------------------------------


def test_resolve_optional() -> None:
    assert resolve_optional("clipforge.does_not_exist_zzz", "f") is None
    assert resolve_optional("clipforge.pipeline", "does_not_exist") is None
    assert resolve_optional("clipforge.heartbeat", "beat") is heartbeat.beat


def test_missing_pipeline_and_purge_are_skipped_quietly(
    settings: Settings, engine: Engine, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(pipeline, "run_once", raising=False)
    monkeypatch.delitem(sys.modules, "clipforge.ingest.purge", raising=False)
    monkeypatch.setattr(worker, "resolve_optional", lambda module, attr: None)
    ctx = make_ctx(settings, engine, notifier)
    worker.job_nightly_pipeline(ctx)  # no exception
    worker.job_purge_sources(ctx)


def test_pipeline_and_purge_are_called_when_they_exist(
    settings: Settings, engine: Engine, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setattr(pipeline, "run_once", lambda s: calls.append(("run_once", (s,))), raising=False)
    fake = types.ModuleType("clipforge.ingest.purge")
    fake.purge_sources = lambda session, s: calls.append(("purge", (type(session).__name__, s)))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clipforge.ingest.purge", fake)
    ctx = make_ctx(settings, engine, notifier)
    worker.job_nightly_pipeline(ctx)
    worker.job_purge_sources(ctx)
    assert calls == [("run_once", (settings,)), ("purge", ("Session", settings))]


# --- jobs, end to end ----------------------------------------------------------


def test_heartbeat_job(settings: Settings, engine: Engine, notifier: FakeNotifier) -> None:
    assert heartbeat.age_s(settings.heartbeat_dir, "worker") is None
    worker.job_heartbeat(make_ctx(settings, engine, notifier))
    assert heartbeat.is_healthy(settings.heartbeat_dir, "worker")


def test_evening_jobs_end_to_end(
    factory: Factory, session: Session, settings: Settings, engine: Engine, notifier: FakeNotifier
) -> None:
    approved_clip(factory, session)
    session.commit()

    worker.job_arm_message(make_ctx(settings, engine, notifier, ARM_AT))
    worker.job_arm_message(make_ctx(settings, engine, notifier, ARM_AT + timedelta(minutes=1)))
    assert len(notifier.sent) == 1  # one Arm message per evening
    assert "arm to publish" in notifier.texts[0]

    from clipforge.review.evening import arm_evening

    arm_evening(session, settings, ARM_AT + timedelta(minutes=2))
    session.commit()

    worker.job_slot_dispatcher(make_ctx(settings, engine, notifier, FIRST_SLOT))
    session.expire_all()
    posts = session.scalars(select(PostRow)).all()
    assert len(posts) == 2
    assert {p.state for p in posts} == {PostState.LIVE}

    worker.job_url_poller(make_ctx(settings, engine, notifier, FIRST_SLOT))
    notices = [t for t in notifier.texts if t.startswith("SUBMIT NOW")]
    assert len(notices) == 2
    worker.job_url_poller(make_ctx(settings, engine, notifier, FIRST_SLOT + timedelta(seconds=30)))
    assert len([t for t in notifier.texts if t.startswith("SUBMIT NOW")]) == 2  # not repeated
    worker.job_submit_reminders(make_ctx(settings, engine, notifier, FIRST_SLOT + timedelta(minutes=21)))
    session.expire_all()
    assert all(s.notice_sent_at is not None for s in session.scalars(select(SubmissionRow)))


def test_unarmed_evening_is_skipped_by_the_dispatcher_job(
    factory: Factory, session: Session, settings: Settings, engine: Engine, notifier: FakeNotifier
) -> None:
    approved_clip(factory, session)
    session.commit()
    worker.job_slot_dispatcher(make_ctx(settings, engine, notifier, FIRST_SLOT))
    session.expire_all()
    assert session.scalars(select(PostRow)).all() == []
    assert any(e.to_state == "evening_skipped" for e in session.scalars(select(EventRow)))
    _ = skip_evening


def test_report_jobs_write_files_and_send_the_text(
    factory: Factory, session: Session, settings: Settings, engine: Engine, notifier: FakeNotifier
) -> None:
    submission(factory, session, real_post(factory, session))
    session.commit()
    saturday = datetime(2026, 11, 28, 13, 0, tzinfo=UTC)
    worker.job_weekly_report(make_ctx(settings, engine, notifier, saturday))
    assert (settings.reports_dir / "weekly-2026-W48.md").exists()
    assert notifier.texts[-1].startswith("# Weekly digest 2026-W48")
    worker.job_monthly_report(make_ctx(settings, engine, notifier, datetime(2026, 12, 6, 13, 0, tzinfo=UTC)))
    assert (settings.reports_dir / "monthly-2026-11.md").exists()
    assert notifier.texts[-1].startswith("# Monthly report 2026-11")
    worker.job_weekly_learning(make_ctx(settings, engine, notifier, saturday))
    worker.job_youtube_views(make_ctx(settings, engine, notifier, saturday))  # no key: does nothing


def test_backup_job(settings: Settings, engine: Engine, notifier: FakeNotifier) -> None:
    upgrade(settings.database_url)
    worker.job_backup_db(make_ctx(settings, engine, notifier))
    assert len(list(settings.backups_dir.glob("clipforge-*.db"))) == 1


def test_backup_failure_is_isolated_and_alerted(
    settings: Settings, engine: Engine, notifier: FakeNotifier, session: Session
) -> None:
    # no database file at settings.db_path -> FileNotFoundError inside the job
    guarded(make_ctx(settings, engine, notifier), JobSpec("backup_db", None, worker.job_backup_db))()  # type: ignore[arg-type]
    session.expire_all()
    assert len(alerts_for(session, "backup_db")) == 1


def test_worker_never_runs_live_in_tests(settings: Settings) -> None:
    assert settings.publish_mode.value != "live"
    assert date(2026, 11, 24)  # keep the date import honest for readers of the fixtures above
