"""Read helpers shared by publish, review and track: row -> schema conversions and clip lineage lookups.

No state changes here. Lineage: clip -> candidate -> source -> campaign.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import ApprovalRow, CampaignRow, CandidateRow, ClipRow, SourceRow
from clipforge.schemas import Approval, Campaign, CampaignIn, Platform


@dataclass(frozen=True)
class Lineage:
    clip: ClipRow
    candidate: CandidateRow
    source: SourceRow
    campaign: CampaignRow


def lineage(session: Session, clip: ClipRow) -> Lineage:
    candidate = session.get(CandidateRow, clip.candidate_id)
    if candidate is None:
        raise LookupError(f"clip {clip.id} has no candidate {clip.candidate_id}")
    source = session.get(SourceRow, candidate.source_id)
    if source is None:
        raise LookupError(f"candidate {candidate.id} has no source {candidate.source_id}")
    campaign = session.get(CampaignRow, source.campaign_id)
    if campaign is None:
        raise LookupError(f"source {source.id} has no campaign {source.campaign_id}")
    return Lineage(clip, candidate, source, campaign)


def campaign_model(row: CampaignRow) -> Campaign:
    """CampaignRow -> schemas.Campaign (the `marketplace` field is stored as `platform_marketplace`)."""
    data: dict[str, Any] = {name: getattr(row, name) for name in CampaignIn.model_fields if name != "marketplace"}
    data["marketplace"] = row.platform_marketplace
    return Campaign.model_validate({**data, "id": row.id, "status": row.status, "created_at": row.created_at})


def approvals_for(session: Session, clip_id: int) -> list[Approval]:
    rows = session.scalars(select(ApprovalRow).where(ApprovalRow.clip_id == clip_id).order_by(ApprovalRow.id)).all()
    return [
        Approval(clip_id=r.clip_id, decision=r.decision, reason=r.reason, decided_at=r.decided_at, actor="operator")
        for r in rows
        if r.actor == "operator"  # only operator decisions count (invariant 5)
    ]


def caption_for(candidate: CandidateRow, platform: Platform) -> str:
    """The stored caption for `platform`, falling back to the hook. Always re-normalized before publishing.

    `captions_json` is read as {platform: text}; a list of {"platform": ..., "text": ...} is accepted too
    (docs/contract-requests/ops.md).
    """
    raw = candidate.captions_json
    text: str | None = None
    if isinstance(raw, Mapping):
        value = raw.get(platform.value)
        if isinstance(value, str):
            text = value
        elif isinstance(value, Mapping) and isinstance(value.get("text"), str):
            text = value["text"]
    elif isinstance(raw, list):
        for item in raw:
            if (
                isinstance(item, Mapping)
                and item.get("platform") == platform.value
                and isinstance(item.get("text"), str)
            ):
                text = item["text"]
                break
    return text if text is not None else (candidate.hook or "")
