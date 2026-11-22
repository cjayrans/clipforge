"""Conversions between DB rows and the `schemas` contract models (so compliance functions get pydantic models)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from clipforge.db import CampaignRow, SourceRow, record_event
from clipforge.db.transitions import Actor
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
    """Change a source's status and write an `events` row (the audit log). `fail_reason` only with FAILED."""
    old = source.status
    source.status = status
    source.fail_reason = fail_reason if status is SourceStatus.FAILED else None
    session.flush()
    record_event(
        session,
        "source",
        source.id,
        old.value if old is not None else None,
        status.value,
        actor=actor,
        detail=detail or (fail_reason or ""),
    )
    session.flush()
