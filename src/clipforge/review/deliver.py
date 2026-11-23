"""Deliver operator alerts to Telegram and watch the worker heartbeat (the bot's background tasks).

Alerts are `events(entity="alert")` rows written by any module (see review.alerts; ingest uses the same convention);
the spend-cap pause (`events(entity="pipeline", to_state="paused")`, written by costs.pause) is delivered too.
A cursor in `flags` (`alerts_cursor` = last delivered event id) makes delivery at-least-once and restart-safe.
"""

from __future__ import annotations

import logging
from datetime import datetime, time

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from clipforge import heartbeat
from clipforge.config import Settings
from clipforge.db import EventRow, session_scope
from clipforge.review import alerts
from clipforge.review.notify import Notifier, NotifyError
from clipforge.review.service import get_flag, set_flag

log = logging.getLogger(__name__)

ALERT_CURSOR = "alerts_cursor"
WATCH_START = time(19, 30)
WATCH_END = time(21, 0)


def alert_text(event: EventRow) -> str:
    if event.entity == "pipeline":
        return f"Pipeline paused: {event.detail}"
    return f"Alert ({event.to_state}): {event.detail}"


async def deliver_pending_alerts(factory: sessionmaker[Session], notifier: Notifier, *, limit: int = 20) -> int:
    with session_scope(factory) as session:
        cursor = int(get_flag(session, ALERT_CURSOR) or 0)
        pending = session.scalars(
            select(EventRow)
            .where(
                EventRow.id > cursor,
                or_(
                    EventRow.entity == alerts.ALERT_ENTITY,
                    and_(EventRow.entity == "pipeline", EventRow.to_state == "paused"),
                ),
            )
            .order_by(EventRow.id)
            .limit(limit)
        ).all()
        items = [(e.id, alert_text(e)) for e in pending]
    sent = 0
    for event_id, text in items:
        try:
            await notifier.send_text(text)
        except NotifyError as exc:
            log.warning("alert %s not delivered (%s); will retry", event_id, exc)
            break
        with session_scope(factory) as session:
            set_flag(session, ALERT_CURSOR, str(event_id))
        sent += 1
    return sent


def worker_is_stale(settings: Settings, now: datetime) -> bool:
    """PRD F14: during 19:30-21:00 local, the worker heartbeat is missing or older than 10 minutes."""
    local = now.astimezone(settings.app.tz)
    if not WATCH_START <= local.time().replace(tzinfo=None) < WATCH_END:
        return False
    age = heartbeat.age_s(settings.heartbeat_dir, "worker", now=now.timestamp())
    return age is None or age > heartbeat.ALERT_AFTER_S


def check_worker_heartbeat(session: Session, settings: Settings, now: datetime) -> bool:
    """Raise a worker_stale alert at most once per evening. Returns True when one was raised."""
    if not worker_is_stale(settings, now):
        return False
    day = now.astimezone(settings.app.tz).date().isoformat()
    key = f"worker_stale_alert:{day}"
    if get_flag(session, key) is not None:
        return False
    set_flag(session, key, now.isoformat())
    alerts.raise_alert(
        session,
        alerts.WORKER_STALE,
        day,
        "The worker heartbeat is older than 10 minutes. Tonight's slots may not publish.",
    )
    return True
