"""Conversions between DB rows and the `schemas` contract models (so compliance functions get pydantic models)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import CampaignRow, CandidateRow, SourceRow, record_event
from clipforge.db.transitions import Actor
from clipforge.ingest.alerts import redact_urls
from clipforge.render.facecam import DETECTOR_VERSION, FacecamDetection
from clipforge.schemas import Campaign, CampaignIn, Source, SourceStatus

# CampaignIn field -> CampaignRow column, where they differ.
_ROW_COLUMN: dict[str, str] = {"marketplace": "platform_marketplace"}


def campaign_row_values(campaign: CampaignIn) -> dict[str, Any]:
    """Column values for a CampaignRow from a validated CampaignIn (JSON-compatible: enums/URLs as strings)."""
    data = campaign.model_dump(mode="json")
    return {_ROW_COLUMN.get(k, k): v for k, v in data.items()}


def campaign_from_row(row: CampaignRow) -> Campaign:
    data: dict[str, Any] = {}
    for name in Campaign.model_fields:
        data[name] = getattr(row, _ROW_COLUMN.get(name, name))
    return Campaign.model_validate(data)


def source_from_row(row: SourceRow) -> Source:
    return Source.model_validate(
        {name: getattr(row, name) for name in Source.model_fields},
    )


def set_source_status(
    session: Session,
    source: SourceRow,
    status: SourceStatus,
    *,
    fail_reason: str | None = None,
    detail: str = "",
    actor: Actor = "system",
) -> None:
    """Change a source's status and write an `events` row (the audit log).

    `fail_reason` is set with FAILED, kept on PURGED, and cleared otherwise. URL query strings are dropped from
    `detail`.
    """
    old = source.status
    source.status = status
    if status is SourceStatus.FAILED:
        source.fail_reason = fail_reason
    elif status is not SourceStatus.PURGED:  # a purged source keeps why it failed
        source.fail_reason = None
    session.flush()
    record_event(
        session,
        "source",
        source.id,
        old.value if old is not None else None,
        status.value,
        actor=actor,
        detail=redact_urls(detail or (fail_reason or "")),
    )
    session.flush()


def facecam_from_row(row: SourceRow) -> FacecamDetection | None:
    """The facecam detection stored on a source, or None when there is none for the current detector version."""
    data = row.facecam_json
    if not isinstance(data, dict):
        return None
    detection = FacecamDetection.from_json(data)
    return detection if detection.version == DETECTOR_VERSION else None


def store_facecam(
    session: Session,
    row: SourceRow,
    detection: FacecamDetection,
    *,
    seconds: float | None = None,
    actor: Actor = "system",
) -> None:
    """Store a facecam detection on the source (found or none) with an `events` row."""
    row.facecam_json = detection.to_json()
    took = f" in {seconds:.0f}s" if seconds is not None else ""
    record_event(
        session,
        "source",
        row.id,
        row.status.value,
        row.status.value,
        actor=actor,
        detail=f"{detection.summary()}{took}",
    )


def clip_windows(session: Session, source_id: int) -> list[tuple[float, float]]:
    """(start_s, end_s) of every candidate of a source, any state: where facecam detection samples frames."""
    rows = session.execute(
        select(CandidateRow.start_s, CandidateRow.end_s).where(CandidateRow.source_id == source_id)
    ).all()
    return [(float(a), float(b)) for a, b in rows if b > a]
