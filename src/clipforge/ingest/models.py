"""Conversions between DB rows and the `schemas` contract models (so compliance functions get pydantic models)."""

from __future__ import annotations

from typing import Any

from clipforge.db import CampaignRow, SourceRow
from clipforge.schemas import Campaign, CampaignIn, Source

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
