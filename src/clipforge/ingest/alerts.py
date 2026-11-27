"""Operator alerts raised by ingest and transcribe.

An alert is an `events` row with `entity="alert"`, `to_state=<kind>` and a human-readable `detail`, plus a WARNING log
line. `alert_operator` delegates to `review.alerts.raise_alert` (one implementation); the bot delivers every alert row
to Telegram (`review.deliver.deliver_pending_alerts`, cursor `flags.alerts_cursor`), which closes CR-A1.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from clipforge.db import EventRow
from clipforge.review.alerts import ALERT_ENTITY, raise_alert, redact_urls

__all__ = ["ALERT_ENTITY", "alert_operator", "redact_urls"]

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
VOD_DISCOVERY_FAILED = "vod_discovery_failed"  # moments-v2: the nightly Twitch VOD lookup failed


def alert_operator(session: Session, kind: str, entity_id: int | str, message: str) -> EventRow:
    """Record an alert for the operator. Query strings of URLs in `message` are removed (they can carry tokens)."""
    return raise_alert(session, kind, entity_id, message)
