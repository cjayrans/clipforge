"""P2.3: New hook / Shorter re-render (bot buttons). A re-render needs a fresh approval (COMPLIANCE #5)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clipforge.compliance import ComplianceError, assert_human_approved
from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, EveningRow, EventRow, FlagRow, PostRow, session_scope, transition
from clipforge.pipeline import (
    PipelineDeps,
    RerenderRefused,
    ReviewHookRunner,
    begin_rerender,
    finish_rerender,
    run_once,
)
from clipforge.publish.dispatcher import dispatch_due_slots
from clipforge.publish.queries import approvals_for
from clipforge.review.evening import arm_evening
from clipforge.review.service import approve_clip, render_stamp
from clipforge.schemas import ClipState
from tests.rank.anthropic_mock import FakeAnthropic
from tests.support.pipeline_fakes import FakeRender, fake_anthropic
from tests.support.pipeline_fixtures import Clock, add_campaign

DAY = date(2026, 11, 23)
APPROVE_AT = datetime(2026, 11, 24, 0, 50, tzinfo=UTC)  # 19:50 ET
ARM_AT = datetime(2026, 11, 24, 1, 1, tzinfo=UTC)
SLOT_AT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)


@pytest.fixture
def reviewed(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, mocked_http: respx.MockRouter
) -> int:
    """One nightly run; returns the id of the first PENDING_REVIEW clip."""
    add_campaign(db, pipe_settings)
    assert run_once(pipe_settings, deps=deps).ready >= 1
    with session_scope(db) as s:
        clip = s.scalars(select(ClipRow).where(ClipRow.state == ClipState.PENDING_REVIEW).order_by(ClipRow.id)).first()
        assert clip is not None
        return clip.id


def _clip(db: sessionmaker[Session], clip_id: int) -> ClipRow:
    with session_scope(db) as s:
        clip = s.get(ClipRow, clip_id)
        assert clip is not None
        return clip


def _approve(db: sessionmaker[Session], clip_id: int, at: datetime = APPROVE_AT) -> None:
    with session_scope(db) as s:
        clip = s.get(ClipRow, clip_id)
        assert clip is not None
        approve_clip(s, clip_id, stamp=render_stamp(clip.rendered_at), now=at)


def _rerender(settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, clip_id: int, kind: str) -> str:
    deps.clock = Clock(APPROVE_AT + timedelta(minutes=5))
    job = begin_rerender(settings, db, clip_id, kind, deps.clock())  # type: ignore[arg-type]
    return finish_rerender(settings, db, job, deps)


def test_new_hook_makes_the_old_approval_stale(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    reviewed: int,
) -> None:
    _approve(db, reviewed)
    before = _clip(db, reviewed)
    anthropic.default = fake_anthropic(["Nobody expected that jump"]).default
    message = _rerender(pipe_settings, db, deps, reviewed, "new_hook")
    assert "new hook: Nobody expected that jump" in message

    after = _clip(db, reviewed)
    assert after.state is ClipState.PENDING_REVIEW
    assert after.rendered_at > before.rendered_at
    assert after.path != before.path
    assert Path(after.path).is_file()
    assert not Path(before.path).exists()  # superseded version folder removed (never posted)
    with session_scope(db) as s:
        cand = s.get(CandidateRow, after.candidate_id)
        assert cand is not None
        assert cand.hook == "Nobody expected that jump"
        assert all(t.startswith("#ad ") for t in (cand.captions_json or {}).values())
        # COMPLIANCE #5: the approval made before the re-render no longer counts.
        with pytest.raises(ComplianceError, match="no operator approval after"):
            assert_human_approved(approvals_for(s, reviewed), after.rendered_at)
        assert s.get(FlagRow, f"rerender:{reviewed}") is None
        assert [e.to_state for e in s.scalars(select(EventRow).where(EventRow.entity == "clip"))][-2:] == [
            "rendered",
            "pending_review",
        ]


def test_rerendered_clip_cannot_publish_on_the_old_approval(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, reviewed: int
) -> None:
    """End to end through the dispatcher: approve, schedule, re-render, arm, slot -> nothing publishes."""
    _approve(db, reviewed)
    _rerender(pipe_settings, db, deps, reviewed, "new_hook")
    with session_scope(db) as s:
        arm_evening(s, pipe_settings, ARM_AT, day=DAY)
    with session_scope(db) as s:
        assert dispatch_due_slots(s, pipe_settings, SLOT_AT) == []
        assert s.scalars(select(PostRow)).all() == []
    assert _clip(db, reviewed).state is ClipState.PENDING_REVIEW


def test_scheduled_clip_is_released_before_re_rendering(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, reviewed: int
) -> None:
    _approve(db, reviewed)
    with session_scope(db) as s:
        arm_evening(s, pipe_settings, ARM_AT, day=DAY)
    assert _clip(db, reviewed).state is ClipState.SCHEDULED
    job = begin_rerender(pipe_settings, db, reviewed, "shorter", ARM_AT + timedelta(minutes=2))
    with session_scope(db) as s:
        evening = s.get(EveningRow, DAY)
        assert evening is not None
        assert all(slot["clip_id"] != reviewed for slot in evening.slots_json)
        states = [e.to_state for e in s.scalars(select(EventRow).where(EventRow.entity == "clip"))]
        assert states[-2:] == ["approved", "rendered"]
    deps.clock = Clock(ARM_AT + timedelta(minutes=3))
    finish_rerender(pipe_settings, db, job, deps)
    assert _clip(db, reviewed).state is ClipState.PENDING_REVIEW


def test_shorter_cuts_at_least_20_percent(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, reviewed: int
) -> None:
    before = _clip(db, reviewed)
    with session_scope(db) as s:
        cand = s.get(CandidateRow, before.candidate_id)
        assert cand is not None
        old_len = cand.end_s - cand.start_s
    message = _rerender(pipe_settings, db, deps, reviewed, "shorter")
    after = _clip(db, reviewed)
    assert "re-rendered" in message
    assert after.duration_s <= old_len * 0.8 + 0.01
    assert after.duration_s >= pipe_settings.app.pipeline.min_clip_s
    with session_scope(db) as s:
        cand = s.get(CandidateRow, after.candidate_id)
        assert cand is not None
        assert cand.end_s - cand.start_s == pytest.approx(after.duration_s, abs=0.01)
        ev = s.scalars(select(EventRow).where(EventRow.entity == "candidate").order_by(EventRow.id.desc())).first()
        assert ev is not None
        assert ev.detail.startswith("shorter:")
        assert ev.actor == "operator"


def test_failed_re_render_keeps_the_old_file_and_needs_a_new_approval(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, render: FakeRender, reviewed: int
) -> None:
    _approve(db, reviewed)
    before = _clip(db, reviewed)
    render.fail = 1
    message = _rerender(pipe_settings, db, deps, reviewed, "shorter")
    assert "failed" in message
    after = _clip(db, reviewed)
    assert after.state is ClipState.PENDING_REVIEW
    assert after.path == before.path
    assert Path(after.path).is_file()
    assert after.rendered_at > before.rendered_at  # the old approval is stale: the operator re-approves
    with session_scope(db) as s:
        kinds = [e.to_state for e in s.scalars(select(EventRow).where(EventRow.entity == "alert"))]
        assert "rerender_failed" in kinds
        assert s.get(FlagRow, f"rerender:{reviewed}") is None


@pytest.mark.parametrize("final", [ClipState.REJECTED, ClipState.QA_FAILED])
def test_finished_clips_cannot_be_re_rendered(
    pipe_settings: Settings, db: sessionmaker[Session], reviewed: int, final: ClipState
) -> None:
    with session_scope(db) as s:
        clip = s.get(ClipRow, reviewed)
        assert clip is not None
        if final is ClipState.REJECTED:
            transition(s, clip, ClipState.REJECTED)
        else:
            transition(s, clip, ClipState.RENDERED)
            transition(s, clip, ClipState.QA_FAILED)
    with pytest.raises(RerenderRefused, match="can no longer be changed"):
        begin_rerender(pipe_settings, db, reviewed, "new_hook", APPROVE_AT)


def test_second_tap_is_refused_while_rendering(
    pipe_settings: Settings, db: sessionmaker[Session], reviewed: int
) -> None:
    begin_rerender(pipe_settings, db, reviewed, "new_hook", APPROVE_AT)
    with pytest.raises(RerenderRefused, match="already being re-rendered"):
        begin_rerender(pipe_settings, db, reviewed, "shorter", APPROVE_AT + timedelta(seconds=5))


async def test_hook_runner_answers_at_once_and_finishes_in_the_background(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, reviewed: int
) -> None:
    deps.clock = Clock(APPROVE_AT)
    runner = ReviewHookRunner(pipe_settings, db, deps)
    reply = await runner.new_hook(reviewed)
    assert "new preview follows" in reply
    assert "already being re-rendered" in await runner.new_hook(reviewed)  # double tap while it runs
    await asyncio.gather(*runner.tasks)
    assert _clip(db, reviewed).state is ClipState.PENDING_REVIEW
    assert reviewed not in runner.busy
    assert "no longer exists" in await runner.shorter(999_999)
