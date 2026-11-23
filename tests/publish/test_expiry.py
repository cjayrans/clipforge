"""Phase 2: expiring a drained/ended campaign's clips (CR-A5), releasing a scheduled clip, invariant 7 dead ends."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ClipRow, EveningRow, EventRow, transition
from clipforge.ingest.registry import set_campaign_status
from clipforge.publish.dispatcher import dispatch_due_slots, expire_campaign_clips, release_clip
from clipforge.schemas import CampaignStatus, ClipState, Platform
from tests.support.factories import Factory

SLOT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)  # 20:10 America/New_York on Mon 23 Nov 2026


def _clip_in(factory: Factory, session: Session, state: ClipState, campaign: object = None) -> ClipRow:
    src = factory.source(campaign) if campaign is not None else factory.source()  # type: ignore[arg-type]
    clip = factory.clip(factory.candidate(src, state=ClipState.CANDIDATE))
    path = {
        ClipState.RENDERED: [],
        ClipState.PENDING_REVIEW: [ClipState.PENDING_REVIEW],
        ClipState.APPROVED: [ClipState.PENDING_REVIEW, ClipState.APPROVED],
        ClipState.SCHEDULED: [ClipState.PENDING_REVIEW, ClipState.APPROVED, ClipState.SCHEDULED],
        ClipState.PUBLISHED: [ClipState.PENDING_REVIEW, ClipState.APPROVED, ClipState.SCHEDULED, ClipState.PUBLISHED],
        ClipState.REJECTED: [ClipState.PENDING_REVIEW, ClipState.REJECTED],
    }[state]
    for s in path:
        transition(session, clip, s)
    return clip


@pytest.mark.parametrize("status", [CampaignStatus.DRAINED, CampaignStatus.ENDED])
def test_drain_or_end_expires_unpublished_clips(
    factory: Factory, session: Session, settings: Settings, status: CampaignStatus
) -> None:
    camp = factory.campaign()
    other = factory.campaign()
    src = factory.source(camp)
    fresh = factory.candidate(src)
    pending = _clip_in(factory, session, ClipState.PENDING_REVIEW, camp)
    approved = _clip_in(factory, session, ClipState.APPROVED, camp)
    scheduled = _clip_in(factory, session, ClipState.SCHEDULED, camp)
    publishing = _clip_in(factory, session, ClipState.SCHEDULED, camp)
    factory.post(publishing)
    published = _clip_in(factory, session, ClipState.PUBLISHED, camp)
    rejected = _clip_in(factory, session, ClipState.REJECTED, camp)
    untouched = _clip_in(factory, session, ClipState.APPROVED, other)
    factory.evening(
        date(2026, 11, 23),
        slots_json=[
            {"at": SLOT.isoformat(), "clip_id": scheduled.id},
            {"at": SLOT.isoformat(), "clip_id": untouched.id},
        ],
    )

    set_campaign_status(session, settings, camp.id, status)

    assert fresh.state is ClipState.EXPIRED
    for clip in (pending, approved, scheduled):
        assert clip.state is ClipState.EXPIRED
    assert publishing.state is ClipState.SCHEDULED  # already has a post: the dispatcher finishes it
    assert published.state is ClipState.PUBLISHED
    assert rejected.state is ClipState.REJECTED
    assert untouched.state is ClipState.APPROVED
    evening = session.get(EveningRow, date(2026, 11, 23))
    assert evening is not None
    assert [s["clip_id"] for s in evening.slots_json] == [None, untouched.id]
    ev = session.scalars(
        select(EventRow).where(EventRow.entity == "clip", EventRow.entity_id == str(pending.id)).order_by(EventRow.id)
    ).all()[-1]
    assert (ev.to_state, ev.actor, ev.detail) == ("expired", "operator", f"campaign {status.value}")


def test_expiry_is_idempotent(factory: Factory, session: Session) -> None:
    camp = factory.campaign(status="ended")
    _clip_in(factory, session, ClipState.APPROVED, camp)
    assert expire_campaign_clips(session, camp.id, "sweep") == 2  # the candidate row and the clip
    assert expire_campaign_clips(session, camp.id, "sweep") == 0


def test_release_clip_from_its_slot(factory: Factory, session: Session) -> None:
    clip = _clip_in(factory, session, ClipState.SCHEDULED)
    factory.evening(date(2026, 11, 23), slots_json=[{"at": SLOT.isoformat(), "clip_id": clip.id}])
    release_clip(session, clip, "re-render", actor="operator")
    assert clip.state is ClipState.APPROVED
    evening = session.get(EveningRow, date(2026, 11, 23))
    assert evening is not None
    assert evening.slots_json[0]["clip_id"] is None


def test_release_refused_once_posts_exist(factory: Factory, session: Session) -> None:
    clip = _clip_in(factory, session, ClipState.SCHEDULED)
    factory.post(clip)
    with pytest.raises(ValueError, match="already has posts"):
        release_clip(session, clip, "re-render")
    assert clip.state is ClipState.SCHEDULED


def test_clip_whose_file_is_already_on_every_platform_expires(
    factory: Factory, session: Session, settings: Settings
) -> None:
    """Ops reviewer note 4: an invariant-7 refusal on every platform used to re-queue the clip every evening."""
    camp = factory.campaign(allowed_platforms=["tiktok", "instagram"])
    twin = _clip_in(factory, session, ClipState.PUBLISHED, camp)
    for p in (Platform.TIKTOK, Platform.INSTAGRAM):
        factory.post(twin, platform=p)
    clip = _clip_in(factory, session, ClipState.SCHEDULED, camp)
    clip.sha256 = twin.sha256  # the same file
    factory.approval(clip, decided_at=clip.rendered_at.replace(hour=7))
    factory.evening(
        date(2026, 11, 23),
        armed_at=datetime(2026, 11, 24, 1, 0, tzinfo=UTC),
        slots_json=[{"at": SLOT.isoformat(), "clip_id": clip.id}],
    )
    session.commit()
    assert dispatch_due_slots(session, settings, SLOT) == []
    assert clip.state is ClipState.EXPIRED
    kinds = [e.to_state for e in session.scalars(select(EventRow).where(EventRow.entity == "alert"))]
    assert kinds.count("gate_refused") == 2
    assert "clip_expired" in kinds
