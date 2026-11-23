"""Operator alerts: written as `events` rows, delivered to Telegram by the bot.

Convention shared with the ingest branch: `events(entity="alert", to_state=<kind>, detail=<text>)`. Writing an alert
never needs Telegram; the bot's alert task (review.bot) delivers pending alerts and keeps a cursor in `flags`.
Never put secrets in `message` (URLs with query strings are shortened).
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from clipforge.db import EventRow, record_event

log = logging.getLogger(__name__)

ALERT_ENTITY = "alert"

# Kinds raised by publish / review / track / scheduler.
EVENING_SKIPPED = "evening_skipped"
SLOT_MISSED = "slot_missed"
PUBLISH_FAILED = "publish_failed"
PUBLISH_UNKNOWN = "publish_unknown"
CLIP_EXPIRED = "clip_expired"
GATE_REFUSED = "gate_refused"
WORKER_STALE = "worker_stale"
JOB_FAILED = "job_failed"
PREVIEW_TOO_BIG = "preview_too_big"

_URL_QUERY_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#]+)\?[^\s#]*", re.IGNORECASE)


def raise_alert(session: Session, kind: str, entity_id: int | str, message: str) -> EventRow:
    """Record an alert for the operator. Flushes via the session; the caller commits."""
    clean = _URL_QUERY_RE.sub(r"\1?…", message)
    log.warning("operator alert [%s] %s: %s", kind, entity_id, clean)
    return record_event(session, ALERT_ENTITY, entity_id, None, kind, actor="system", detail=clean)
