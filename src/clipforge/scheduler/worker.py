"""APScheduler worker: every job in docs/ARCHITECTURE.md §4.9 (`cf worker`).

Jobs (America/New_York):

- nightly_pipeline: 01:00 daily, `clipforge.pipeline.run_once(settings)` (Phase 2)
- purge_sources: 03:30 daily, `clipforge.ingest.purge.purge_sources` (branch A)
- backup_db: 04:00 daily, SQLite online backup, keep N
- youtube_views: 12:00 daily, view snapshots (needs YOUTUBE_API_KEY)
- arm_message: every 60 s, acts once per evening: Arm / Skip message between its send time and the first slot
- slot_dispatcher: every 30 s, publish due slots on an armed evening
- url_poller: every 30 s, poll Upload-Post, then send Submit-now messages
- submit_reminders: every 60 s, Submit-now safety net and the +20 min reminders
- weekly_report: Sat 08:00, digest to data/reports/ and Telegram
- weekly_learning: Sat 08:15, few-shots for the ranking prompt
- monthly_report: first Sunday 08:00, monthly report with the kill/scale status
- heartbeat: every 60 s, `data/heartbeat/worker`

`arm_message` runs every minute instead of once at 20:00 so a failed send is retried until the first slot; the function
itself sends at most one message per evening (review.evening.send_arm_message). Jobs the Phase 2 / branch A code does
not provide yet (`pipeline.run_once`, `ingest.purge.purge_sources`) are resolved lazily and skipped with a log line.

Every job runs in its own session with error isolation: an exception is logged, becomes an operator alert (at most one
per job per 30 minutes) and never stops the scheduler.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.orm import Session, sessionmaker

from clipforge import heartbeat
from clipforge.config import Settings
from clipforge.db import make_engine, make_session_factory, session_scope
from clipforge.db.backup import backup_db
from clipforge.db.migrate import upgrade
from clipforge.publish.dispatcher import dispatch_due_slots, poll_pending_posts
from clipforge.publish.upload_post import UploadPostClient
from clipforge.review import alerts
from clipforge.review.evening import send_arm_message, send_reminders, send_submission_notices
from clipforge.review.notify import Notifier, clip_text, notifier_from_settings, run_sync
from clipforge.review.service import get_flag, set_flag
from clipforge.track.learning import run_weekly_learning
from clipforge.track.reports import monthly_report, weekly_report
from clipforge.track.youtube_views import YouTubeClient, snapshot_youtube_views

log = logging.getLogger(__name__)

ALERT_THROTTLE = timedelta(minutes=30)
DAILY_MISFIRE_S = 3600  # a daily job still runs if the machine woke up within an hour of its time
TICK_MISFIRE_S = 60


@dataclass
class WorkerContext:
    settings: Settings
    factory: sessionmaker[Session]
    notifier: Notifier
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    upload_client: UploadPostClient | None = None
    youtube_client: YouTubeClient | None = None


@dataclass(frozen=True)
class JobSpec:
    id: str
    trigger: BaseTrigger
    func: Callable[[WorkerContext], None]
    misfire_grace_s: int = TICK_MISFIRE_S


def resolve_optional(module: str, attr: str) -> Callable[..., Any] | None:
    """A function another phase provides, or None while it does not exist yet."""
    try:
        fn = getattr(importlib.import_module(module), attr, None)
    except ModuleNotFoundError as exc:
        if exc.name is not None and module.startswith(exc.name):
            return None
        raise
    return fn if callable(fn) else None


# --------------------------------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------------------------------


def job_heartbeat(ctx: WorkerContext) -> None:
    heartbeat.beat(ctx.settings.heartbeat_dir, "worker")


def job_slot_dispatcher(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        dispatch_due_slots(session, ctx.settings, ctx.clock(), client=ctx.upload_client)


def _send_notices(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        run_sync(send_submission_notices(session, ctx.settings, ctx.notifier, ctx.clock()))


def job_url_poller(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        poll_pending_posts(session, ctx.settings, ctx.clock(), client=ctx.upload_client)
    _send_notices(ctx)  # Submit-now goes out as soon as a URL (real or dry-run) exists


def job_submit_reminders(ctx: WorkerContext) -> None:
    _send_notices(ctx)  # safety net if the poller's send failed
    with session_scope(ctx.factory) as session:
        run_sync(send_reminders(session, ctx.settings, ctx.notifier, ctx.clock()))


def job_arm_message(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        run_sync(send_arm_message(session, ctx.settings, ctx.notifier, ctx.clock()))


def job_youtube_views(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        written = snapshot_youtube_views(session, ctx.settings, ctx.clock(), client=ctx.youtube_client)
    log.info("youtube view snapshots written: %d", written)


def job_backup_db(ctx: WorkerContext) -> None:
    path = backup_db(ctx.settings.db_path, ctx.settings.backups_dir, ctx.settings.app.retention.backups_keep)
    log.info("database backup written: %s", path.name)


def job_purge_sources(ctx: WorkerContext) -> None:
    purge = resolve_optional("clipforge.ingest.purge", "purge_sources")
    if purge is None:
        log.info("purge_sources: clipforge.ingest.purge is not available yet; skipping")
        return
    with session_scope(ctx.factory) as session:
        purge(session, ctx.settings)


def job_nightly_pipeline(ctx: WorkerContext) -> None:
    run_once = resolve_optional("clipforge.pipeline", "run_once")
    if run_once is None:
        log.info("nightly_pipeline: clipforge.pipeline.run_once is not available yet (Phase 2); skipping")
        return
    run_once(ctx.settings)


def _send_report(ctx: WorkerContext, path_text: str) -> None:
    run_sync(ctx.notifier.send_text(clip_text(path_text, 4000)))


def job_weekly_report(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        path = weekly_report(session, ctx.settings, now=ctx.clock())
    _send_report(ctx, path.read_text(encoding="utf-8"))


def job_weekly_learning(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        result = run_weekly_learning(session, ctx.settings, ctx.clock())
    log.info("weekly learning %s: %d positive, %d negative", result.week, result.positives, result.negatives)


def job_monthly_report(ctx: WorkerContext) -> None:
    with session_scope(ctx.factory) as session:
        path = monthly_report(session, ctx.settings, now=ctx.clock())
    _send_report(ctx, path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------------------------------


def build_jobs(settings: Settings) -> list[JobSpec]:
    """The job table (ARCHITECTURE §4.9). Pure: no I/O, so tests can inspect every trigger."""
    tz = settings.app.tz
    daily = DAILY_MISFIRE_S

    def cron(**fields: Any) -> CronTrigger:
        return CronTrigger(timezone=tz, **fields)

    def every(seconds: int) -> IntervalTrigger:
        return IntervalTrigger(seconds=seconds, timezone=tz)

    return [
        JobSpec("nightly_pipeline", cron(hour=1, minute=0), job_nightly_pipeline, daily),
        JobSpec("purge_sources", cron(hour=3, minute=30), job_purge_sources, daily),
        JobSpec("backup_db", cron(hour=4, minute=0), job_backup_db, daily),
        JobSpec("youtube_views", cron(hour=12, minute=0), job_youtube_views, daily),
        JobSpec("arm_message", every(60), job_arm_message),
        JobSpec("slot_dispatcher", every(30), job_slot_dispatcher),
        JobSpec("url_poller", every(30), job_url_poller),
        JobSpec("submit_reminders", every(60), job_submit_reminders),
        JobSpec("weekly_report", cron(day_of_week="sat", hour=8, minute=0), job_weekly_report, daily),
        JobSpec("weekly_learning", cron(day_of_week="sat", hour=8, minute=15), job_weekly_learning, daily),
        JobSpec("monthly_report", cron(day_of_week="sun", day="1-7", hour=8, minute=0), job_monthly_report, daily),
        JobSpec("heartbeat", every(60), job_heartbeat),
    ]


def _redact(text: str, settings: Settings) -> str:
    for secret in settings.secrets.secret_values():
        text = text.replace(secret, "***")
    return text


def _alert_failure(ctx: WorkerContext, job_id: str, exc: Exception) -> None:
    """At most one operator alert per job per 30 minutes (a job that fails every 30 s must not flood Telegram)."""
    now = ctx.clock()
    with session_scope(ctx.factory) as session:
        key = f"job_failed_alert:{job_id}"
        last = get_flag(session, key)
        if last is not None and now - datetime.fromisoformat(last) < ALERT_THROTTLE:
            return
        set_flag(session, key, now.isoformat())
        text = _redact(f"{type(exc).__name__}: {exc}", ctx.settings)[:300]
        alerts.raise_alert(session, alerts.JOB_FAILED, job_id, f"Job {job_id} failed: {text}")


def guarded(ctx: WorkerContext, spec: JobSpec) -> Callable[[], None]:
    """Wrap a job: errors are logged and alerted, never raised into the scheduler."""

    def run() -> None:
        try:
            spec.func(ctx)
        except Exception as exc:
            log.exception("job %s failed", spec.id)
            try:
                _alert_failure(ctx, spec.id, exc)
            except Exception:
                log.exception("could not record the failure of job %s", spec.id)

    run.__name__ = spec.id
    return run


def make_scheduler(ctx: WorkerContext) -> BlockingScheduler:
    """All jobs registered, not started."""
    scheduler = BlockingScheduler(timezone=ctx.settings.app.tz, job_defaults={"coalesce": True, "max_instances": 1})
    for spec in build_jobs(ctx.settings):
        scheduler.add_job(
            guarded(ctx, spec),
            spec.trigger,
            id=spec.id,
            name=spec.id,
            misfire_grace_time=spec.misfire_grace_s,
            replace_existing=True,
        )
    return scheduler


def run_worker(settings: Settings) -> None:
    """`cf worker`: run the scheduler until stopped (SIGINT / SIGTERM)."""
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        ctx = WorkerContext(
            settings=settings,
            factory=make_session_factory(engine),
            notifier=notifier_from_settings(settings),
            upload_client=UploadPostClient.from_settings(settings),
        )
        scheduler = make_scheduler(ctx)
        mode = settings.publish_mode.value
        if mode == "live":
            log.warning("worker starting in LIVE publish mode: approved clips on armed evenings go out publicly")
        else:
            log.info("worker starting (publish mode %s)", mode)
        heartbeat.beat(settings.heartbeat_dir, "worker")

        def stop(_signum: int, _frame: object) -> None:
            log.info("worker stopping")
            scheduler.shutdown(wait=False)

        signal.signal(signal.SIGTERM, stop)
        with contextlib.suppress(KeyboardInterrupt, SystemExit):
            scheduler.start()
    finally:
        engine.dispose()
