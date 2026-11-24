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
POST_URL_READY = "post_url_ready"
CLIP_EXPIRED = "clip_expired"
GATE_REFUSED = "gate_refused"
WORKER_STALE = "worker_stale"
JOB_FAILED = "job_failed"
PREVIEW_TOO_BIG = "preview_too_big"

# Kinds raised by the nightly pipeline and the New hook / Shorter re-render (Phase 2).
PIPELINE_STAGE_FAILED = "pipeline_stage_failed"
PIPELINE_EMPTY = "pipeline_empty"
RANK_EMPTY = "rank_empty"
RENDER_FAILED = "render_failed"
RERENDER_FAILED = "rerender_failed"

_URL_QUERY_RE = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#]+)\?[^\s#]*", re.IGNORECASE)


def redact_urls(text: str) -> str:
    """Drop query strings from URLs in `text` (signed links, Dropbox `rlkey`), keeping scheme, host and path."""
    return _URL_QUERY_RE.sub(r"\1?…", text)


def raise_alert(session: Session, kind: str, entity_id: int | str, message: str) -> EventRow:
    """Record an alert for the operator. Flushes; the caller commits. The one implementation every module uses
    (ingest.alerts.alert_operator delegates here); the bot delivers it (review.deliver)."""
    clean = redact_urls(message)
    log.warning("operator alert [%s] %s: %s", kind, entity_id, clean)
    ev = record_event(session, ALERT_ENTITY, entity_id, None, kind, actor="system", detail=clean)
    session.flush()
    return ev
