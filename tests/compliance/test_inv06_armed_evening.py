"""Invariant 6: nothing publishes on an evening that is not armed."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.compliance import ComplianceError, assert_evening_armed
from clipforge.config import Settings
from clipforge.db import PostRow, transition
from clipforge.schemas import ClipState, Evening
from tests.compliance._helpers import xfail_until
from tests.support.factories import Factory

ARMED_AT = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)


def test_unarmed_evening_publishes_nothing() -> None:
    with pytest.raises(ComplianceError):
        assert_evening_armed(None)
    with pytest.raises(ComplianceError):
        assert_evening_armed(Evening(date=date(2026, 11, 23)))
    with pytest.raises(ComplianceError):
        assert_evening_armed(Evening(date=date(2026, 11, 23), armed_at=ARMED_AT, skipped=True))
    assert_evening_armed(Evening(date=date(2026, 11, 23), armed_at=ARMED_AT))


@xfail_until("Phase 1 D: clipforge.publish.dispatcher.dispatch_due_slots")
def test_dispatcher_unarmed_evening(factory: Factory, session: Session, settings: Settings) -> None:
    from clipforge.publish.dispatcher import dispatch_due_slots  # type: ignore[attr-defined]

    clip = factory.clip()
    transition(session, clip, ClipState.PENDING_REVIEW)
    factory.approval(clip)
    transition(session, clip, ClipState.APPROVED, actor="operator")
    factory.evening(date(2026, 11, 23))  # not armed
    dispatch_due_slots(session, settings, now=datetime(2026, 11, 24, 1, 10, tzinfo=UTC))  # 20:10 ET
    assert session.scalars(select(PostRow)).all() == []
