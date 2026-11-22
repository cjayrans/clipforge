"""State changes only through db.transitions.transition(), which enforces the schemas tables and writes events."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import EventRow, IllegalTransition, transition
from clipforge.db.models import ClipRow, PostRow
from clipforge.schemas import ClipState, PostState, SubmissionState
from tests.support.factories import Factory


def test_allowed_clip_transition_writes_event(factory: Factory, session: Session) -> None:
    clip = factory.clip()
    transition(session, clip, ClipState.PENDING_REVIEW)
    transition(session, clip, ClipState.APPROVED, actor="operator", detail="looks good")
    assert clip.state is ClipState.APPROVED
    events = session.scalars(select(EventRow).where(EventRow.entity == "clip").order_by(EventRow.id)).all()
    assert [(e.from_state, e.to_state, e.actor) for e in events] == [
        ("rendered", "pending_review", "system"),
        ("pending_review", "approved", "operator"),
    ]
    assert events[1].entity_id == str(clip.id)


def test_illegal_transition_refused(factory: Factory, session: Session) -> None:
    clip = factory.clip()
    with pytest.raises(IllegalTransition):
        transition(session, clip, ClipState.PUBLISHED)  # RENDERED -> PUBLISHED skips approval
    assert clip.state is ClipState.RENDERED
    assert session.scalars(select(EventRow)).all() == []


def test_terminal_state_is_final(factory: Factory, session: Session) -> None:
    clip = factory.clip()
    transition(session, clip, ClipState.QA_FAILED)
    with pytest.raises(IllegalTransition):
        transition(session, clip, ClipState.PENDING_REVIEW)


def test_direct_assignment_refused(factory: Factory) -> None:
    clip = factory.clip()
    with pytest.raises(IllegalTransition, match="via db.transitions"):
        clip.state = ClipState.PENDING_REVIEW


def test_initial_state_enforced() -> None:
    with pytest.raises(IllegalTransition, match="must start"):
        ClipRow(state=ClipState.APPROVED)
    with pytest.raises(IllegalTransition, match="must start"):
        PostRow(state=PostState.LIVE)


def test_candidate_post_submission_tables(factory: Factory, session: Session) -> None:
    cand = factory.candidate()
    transition(session, cand, ClipState.RENDERED)
    post = factory.post()
    transition(session, post, PostState.PUBLISHING)
    transition(session, post, PostState.LIVE)
    with pytest.raises(IllegalTransition):
        transition(session, post, PostState.QUEUED)
    sub = factory.submission(post)
    transition(session, sub, SubmissionState.SUBMITTED, actor="operator")
    with pytest.raises(IllegalTransition):
        transition(session, sub, SubmissionState.MISSED)


def test_same_state_refused(factory: Factory, session: Session) -> None:
    clip = factory.clip()
    with pytest.raises(IllegalTransition, match="already"):
        transition(session, clip, ClipState.RENDERED)
