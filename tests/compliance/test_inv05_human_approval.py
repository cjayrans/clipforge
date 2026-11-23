"""Invariant 5: no publish without an operator `approve` made after the clip's latest render."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clipforge.compliance import ComplianceError, assert_human_approved
from clipforge.config import Settings
from clipforge.db import ClipRow, IllegalTransition, PostRow, session_scope, transition
from clipforge.pipeline import PipelineDeps, begin_rerender, finish_rerender, run_once
from clipforge.publish.dispatcher import dispatch_due_slots
from clipforge.publish.queries import approvals_for
from clipforge.review.evening import arm_evening
from clipforge.review.service import approve_clip, render_stamp
from clipforge.schemas import Approval, ApprovalDecision, ClipState, RejectReason
from tests.support.factories import Factory
from tests.support.pipeline_fixtures import add_campaign

RENDERED = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
APPROVED_AT = datetime(2026, 11, 24, 0, 50, tzinfo=UTC)  # 19:50 ET
ARMED_AT = datetime(2026, 11, 24, 1, 1, tzinfo=UTC)  # 20:01 ET
SLOT_AT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)  # 20:10 ET


def _a(decision: ApprovalDecision, minutes: int) -> Approval:
    reason = RejectReason.BORING if decision is ApprovalDecision.REJECT else None
    return Approval(clip_id=1, decision=decision, reason=reason, decided_at=RENDERED + timedelta(minutes=minutes))


def test_publish_requires_fresh_human_approval() -> None:
    approve = _a(ApprovalDecision.APPROVE, 5)
    assert assert_human_approved([approve], RENDERED) == approve
    with pytest.raises(ComplianceError, match="no operator approval"):
        assert_human_approved([], RENDERED)
    # Approved, then re-rendered (New hook / Shorter): the old approval is stale.
    with pytest.raises(ComplianceError, match="no operator approval"):
        assert_human_approved([approve], RENDERED + timedelta(minutes=10))
    # The latest decision wins.
    with pytest.raises(ComplianceError, match="reject"):
        assert_human_approved([approve, _a(ApprovalDecision.REJECT, 9)], RENDERED)
    later = _a(ApprovalDecision.APPROVE, 12)
    assert assert_human_approved([_a(ApprovalDecision.REJECT, 9), later], RENDERED) == later


def test_approval_exactly_at_render_time_is_not_fresh() -> None:
    with pytest.raises(ComplianceError):
        assert_human_approved([_a(ApprovalDecision.APPROVE, 0)], RENDERED)


def test_naive_datetimes_refused() -> None:
    with pytest.raises(ComplianceError, match="timezone-aware"):
        assert_human_approved([_a(ApprovalDecision.APPROVE, 5)], RENDERED.replace(tzinfo=None))


def test_only_operator_actor_exists() -> None:
    with pytest.raises(ValueError, match="operator"):
        Approval.model_validate({"clip_id": 1, "decision": "approve", "decided_at": RENDERED, "actor": "system"})


def test_clip_cannot_be_scheduled_without_approval_state(factory: Factory, session: Session) -> None:
    clip = factory.clip()
    transition(session, clip, ClipState.PENDING_REVIEW)
    with pytest.raises(IllegalTransition, match="not allowed"):
        transition(session, clip, ClipState.SCHEDULED)


def test_dispatcher_skips_stale_approval(factory: Factory, session: Session, settings: Settings) -> None:
    from clipforge.publish.dispatcher import dispatch_due_slots  # type: ignore[attr-defined]

    clip = factory.clip()
    transition(session, clip, ClipState.PENDING_REVIEW)
    factory.approval(clip, decided_at=clip.rendered_at - timedelta(minutes=1))  # stale
    transition(session, clip, ClipState.APPROVED)
    factory.evening(armed_at=datetime(2026, 11, 24, 1, 0, tzinfo=UTC))
    dispatch_due_slots(session, settings, now=datetime(2026, 11, 24, 1, 10, tzinfo=UTC))
    assert session.scalars(select(PostRow).where(PostRow.clip_id == clip.id)).all() == []
    assert session.get(ClipRow, clip.id).state is not ClipState.PUBLISHED  # type: ignore[union-attr]


def test_rerender_makes_earlier_approval_stale(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    mocked_http: respx.MockRouter,
) -> None:
    """Phase 2: New hook / Shorter set a newer `rendered_at`, so the approval given before them no longer counts and
    the dispatcher publishes nothing until the operator approves the new render (docs/contract-requests/ops.md 4)."""
    add_campaign(db, pipe_settings)
    assert run_once(pipe_settings, deps=deps).ready >= 1
    with session_scope(db) as s:
        clip = s.scalars(select(ClipRow).where(ClipRow.state == ClipState.PENDING_REVIEW)).first()
        assert clip is not None
        clip_id = clip.id
        approve_clip(s, clip_id, stamp=render_stamp(clip.rendered_at), now=APPROVED_AT)
    job = begin_rerender(pipe_settings, db, clip_id, "new_hook", APPROVED_AT + timedelta(minutes=1))
    finish_rerender(pipe_settings, db, job, deps)
    with session_scope(db) as s:
        clip = s.get(ClipRow, clip_id)
        assert clip is not None
        assert clip.state is ClipState.PENDING_REVIEW
        with pytest.raises(ComplianceError):
            assert_human_approved(approvals_for(s, clip_id), clip.rendered_at)
    with session_scope(db) as s:
        arm_evening(s, pipe_settings, ARMED_AT, day=date(2026, 11, 23))
    with session_scope(db) as s:
        assert dispatch_due_slots(s, pipe_settings, SLOT_AT) == []
        assert s.scalars(select(PostRow)).all() == []
