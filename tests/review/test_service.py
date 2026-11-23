"""D1: operator actions on the DB."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ApprovalRow, ClipRow, EveningRow, EventRow, transition
from clipforge.review.service import StaleAction, approve_clip, reject_clip, render_stamp, skip_tonight
from clipforge.schemas import ApprovalDecision, ClipState, RejectReason
from tests.support.factories import Factory

RENDERED = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
STAMP = render_stamp(RENDERED)


def pending(factory: Factory, session: Session) -> ClipRow:
    clip = factory.clip(rendered_at=RENDERED)
    transition(session, clip, ClipState.PENDING_REVIEW)
    return clip


def test_approve_writes_operator_approval_and_transitions(factory: Factory, session: Session) -> None:
    clip = pending(factory, session)
    row = approve_clip(session, clip.id, stamp=STAMP, now=RENDERED + timedelta(minutes=3), message_id=77)
    assert row.decision is ApprovalDecision.APPROVE
    assert row.actor == "operator"
    assert row.decided_at == RENDERED + timedelta(minutes=3)
    assert row.telegram_message_id == 77
    assert clip.state is ClipState.APPROVED
    ev = session.scalars(select(EventRow).where(EventRow.entity == "clip").order_by(EventRow.id.desc())).first()
    assert ev is not None
    assert ev.to_state == str(ClipState.APPROVED)
    assert ev.actor == "operator"


def test_double_tap_is_stale(factory: Factory, session: Session) -> None:
    clip = pending(factory, session)
    approve_clip(session, clip.id, stamp=STAMP, now=RENDERED + timedelta(minutes=3))
    with pytest.raises(StaleAction, match="not waiting for review"):
        approve_clip(session, clip.id, stamp=STAMP, now=RENDERED + timedelta(minutes=4))
    assert len(session.scalars(select(ApprovalRow)).all()) == 1


def test_unknown_clip_is_stale(session: Session) -> None:
    with pytest.raises(StaleAction, match="no longer exists"):
        approve_clip(session, 999, stamp=STAMP, now=RENDERED)


def test_approval_cannot_predate_the_render(factory: Factory, session: Session) -> None:
    clip = pending(factory, session)
    with pytest.raises(StaleAction, match="re-rendered"):
        approve_clip(session, clip.id, stamp=STAMP, now=RENDERED)
    assert clip.state is ClipState.PENDING_REVIEW


def test_buttons_of_an_old_preview_cannot_act_on_a_new_render(factory: Factory, session: Session) -> None:
    clip = pending(factory, session)
    # New hook / Shorter: re-rendered later, back in review with a newer render
    transition(session, clip, ClipState.RENDERED)
    clip.rendered_at = RENDERED + timedelta(hours=1)
    transition(session, clip, ClipState.PENDING_REVIEW)
    later = RENDERED + timedelta(hours=2)
    with pytest.raises(StaleAction, match="re-rendered"):
        approve_clip(session, clip.id, stamp=STAMP, now=later)  # the old message's Approve
    with pytest.raises(StaleAction, match="re-rendered"):
        reject_clip(session, clip.id, RejectReason.BORING, stamp=STAMP, now=later)
    assert clip.state is ClipState.PENDING_REVIEW
    assert session.scalars(select(ApprovalRow)).all() == []
    approve_clip(session, clip.id, stamp=render_stamp(clip.rendered_at), now=later)  # the new preview's Approve
    assert clip.state is ClipState.APPROVED


def test_render_stamp_is_whole_seconds_and_distinguishes_renders() -> None:
    assert render_stamp(RENDERED) == render_stamp(RENDERED + timedelta(milliseconds=400))
    assert render_stamp(RENDERED) != render_stamp(RENDERED + timedelta(seconds=1))


def test_reject_records_reason(factory: Factory, session: Session) -> None:
    clip = pending(factory, session)
    row = reject_clip(session, clip.id, RejectReason.BAD_CROP, stamp=STAMP, now=RENDERED + timedelta(minutes=1))
    assert row.decision is ApprovalDecision.REJECT
    assert row.reason is RejectReason.BAD_CROP
    assert clip.state is ClipState.REJECTED


def test_approve_after_reject_is_stale(factory: Factory, session: Session) -> None:
    clip = pending(factory, session)
    reject_clip(session, clip.id, RejectReason.BORING, stamp=STAMP, now=RENDERED + timedelta(minutes=1))
    with pytest.raises(StaleAction):
        approve_clip(session, clip.id, stamp=STAMP, now=RENDERED + timedelta(minutes=2))


def test_skip_tonight(session: Session, settings: Settings) -> None:
    now = datetime(2026, 11, 24, 0, 30, tzinfo=UTC)  # 19:30 ET on 2026-11-23
    skip_tonight(session, settings, now)
    row = session.get(EveningRow, now.astimezone(settings.app.tz).date())
    assert row is not None
    assert row.skipped
    with pytest.raises(StaleAction, match="already skipped"):
        skip_tonight(session, settings, now)
