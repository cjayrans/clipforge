"""Operator alerts raised by ingest and transcribe.

An alert is an `events` row with `entity="alert"`, `to_state=<kind>` and a human-readable `detail`, plus a WARNING log
line. Delivery to Telegram belongs to the bot (Phase 1 D / Phase 2); see docs/contract-requests/ingest-transcribe.md.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from clipforge.db import EventRow, record_event

log = logging.getLogger(__name__)

ALERT_ENTITY = "alert"

# Alert kinds used by this branch.
HOST_REFUSED = "host_refused"
NOT_REGISTERED = "not_registered"
CAMPAIGN_INACTIVE = "campaign_inactive"
NO_SPEECH = "no_speech"
DOWNLOAD_FAILED = "download_failed"
DUPLICATE_SOURCE = "duplicate_source"
INBOX_UNKNOWN = "inbox_unknown_campaign"
TRANSCRIBE_FAILED = "transcribe_failed"
TRANSCRIBE_FALLBACK = "transcribe_fallback"


def alert_operator(session: Session, kind: str, entity_id: int | str, message: str) -> EventRow:
    """Record an alert for the operator. Never put secrets in `message`."""
    log.warning("operator alert [%s] %s: %s", kind, entity_id, message)
    ev = record_event(session, ALERT_ENTITY, entity_id, None, kind, actor="system", detail=message)
    session.flush()
    return ev
