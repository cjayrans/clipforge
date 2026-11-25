"""`cf clip relayout` (feat/reframe): re-render clips waiting for review with the current layout code.

Only PENDING_REVIEW clips; same window, hook and captions; a fresh rendered_at (old approvals stale, a new preview is
due); the source's facecam is detected once and stored; a failure keeps the old file."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, EventRow, SourceRow, session_scope
from clipforge.pipeline import PipelineDeps, relayout_clips, relayout_one, run_once
from clipforge.render.facecam import FacecamDetection, Presence
from clipforge.review.previews import clips_needing_preview, preview_flag
from clipforge.review.service import set_flag
from clipforge.schemas import ClipState, PxBox
from tests.support.pipeline_fakes import FakeRender
from tests.support.pipeline_fixtures import add_campaign

FOUND = FacecamDetection(
    status="found",
    src_w=1920,
    src_h=1080,
    hits=31,
    samples=40,
    box=PxBox(x=1400, y=700, w=444, h=264),
    face=PxBox(x=1572, y=764, w=100, h=110),
)


class TickingClock:
    """Each call is one minute later (every re-render gets its own r<stamp> folder, as in production)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


@pytest.fixture
def pending(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, mocked_http: respx.MockRouter
) -> list[int]:
    """One nightly run of a game campaign (no facecam known yet); ids of the PENDING_REVIEW clips."""
    add_campaign(db, pipe_settings, content_kind="game")
    assert run_once(pipe_settings, deps=deps).ready >= 2
    deps.clock = TickingClock(deps.clock())
    with session_scope(db) as s:
        ids = list(s.scalars(select(ClipRow.id).where(ClipRow.state == ClipState.PENDING_REVIEW).order_by(ClipRow.id)))
        for cid in ids:  # the bot already sent the first previews
            clip = s.get(ClipRow, cid)
            assert clip is not None
            set_flag(s, preview_flag(cid), clip.rendered_at.isoformat())
    return ids


def _snapshot(db: sessionmaker[Session], clip_id: int) -> tuple[ClipRow, CandidateRow]:
    with session_scope(db) as s:
        clip = s.get(ClipRow, clip_id)
        assert clip is not None
        cand = s.get(CandidateRow, clip.candidate_id)
        assert cand is not None
        s.expunge_all()
        return clip, cand


def test_first_render_without_a_facecam_is_a_crop(db: sessionmaker[Session], pending: list[int]) -> None:
    clip, _ = _snapshot(db, pending[0])
    assert clip.layout == "crop"
    assert clip.render_s is not None


def test_relayout_moves_a_pending_clip_to_stacked_and_keeps_its_copy(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, render: FakeRender, pending: list[int]
) -> None:
    render.facecam_result = FOUND
    render.presence_result = Presence(ratio=0.9, samples=60, face=PxBox(x=172, y=64, w=100, h=110))
    render.face_ratio = 0.9
    before, cand_before = _snapshot(db, pending[0])
    with session_scope(db) as s:
        assert pending[0] not in clips_needing_preview(s)

    r = relayout_one(pipe_settings, db, pending[0], deps)

    assert r.ok, r.message
    assert (r.old_layout, r.new_layout) == ("crop", "stacked")
    after, cand_after = _snapshot(db, pending[0])
    assert after.state is ClipState.PENDING_REVIEW
    assert after.layout == "stacked"
    assert after.rendered_at > before.rendered_at
    assert after.path != before.path
    assert not Path(before.path).exists()
    assert (cand_after.start_s, cand_after.end_s) == (cand_before.start_s, cand_before.end_s)
    assert cand_after.hook == cand_before.hook
    assert cand_after.captions_json == cand_before.captions_json
    assert cand_after.title == cand_before.title
    with session_scope(db) as s:
        assert pending[0] in clips_needing_preview(s)  # the bot resends the preview
        details = [e.detail for e in s.scalars(select(EventRow).where(EventRow.entity == "candidate"))]
        assert any(d.startswith("relayout: crop -> stacked") for d in details)


def test_facecam_is_detected_once_per_source(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, render: FakeRender, pending: list[int]
) -> None:
    render.facecam_result = FOUND
    calls_after_nightly = len(render.facecam_calls)  # the nightly run tried (fake: could not run, nothing stored)
    assert calls_after_nightly >= 1
    for cid in pending[:2]:
        assert relayout_one(pipe_settings, db, cid, deps).ok
    assert len(render.facecam_calls) == calls_after_nightly + 1
    with session_scope(db) as s:
        src = s.scalars(select(SourceRow)).one()
        assert src.facecam_json is not None
        assert src.facecam_json["status"] == "found"
        events = [e.detail for e in s.scalars(select(EventRow).where(EventRow.entity == "source"))]
        assert any(d.startswith("facecam x 1400, y 700, 444x264 px") for d in events)


def test_a_stored_none_result_is_not_detected_again(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, render: FakeRender, pending: list[int]
) -> None:
    render.facecam_result = FacecamDetection(status="none", src_w=1920, src_h=1080, hits=3, samples=40)
    calls = len(render.facecam_calls)
    assert relayout_one(pipe_settings, db, pending[0], deps).new_layout == "crop"
    assert relayout_one(pipe_settings, db, pending[1], deps).new_layout == "crop"
    assert len(render.facecam_calls) == calls + 1


@pytest.mark.parametrize(
    "state", [ClipState.APPROVED, ClipState.SCHEDULED, ClipState.PUBLISHED, ClipState.REJECTED, ClipState.QA_FAILED]
)
def test_relayout_never_touches_other_states(
    state: ClipState,
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    render: FakeRender,
    pending: list[int],
) -> None:
    with session_scope(db) as s:
        s.execute(update(ClipRow).where(ClipRow.id == pending[0]).values(state=state))
    before, _ = _snapshot(db, pending[0])
    encodes = len(render.encodes)
    r = relayout_one(pipe_settings, db, pending[0], deps)
    assert not r.ok
    after, _ = _snapshot(db, pending[0])
    assert (after.state, after.path, after.rendered_at) == (state, before.path, before.rendered_at)
    assert len(render.encodes) == encodes  # nothing was rendered


def test_failed_relayout_keeps_the_old_file(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, render: FakeRender, pending: list[int]
) -> None:
    before, _ = _snapshot(db, pending[0])
    render.fail = 1
    r = relayout_one(pipe_settings, db, pending[0], deps)
    assert not r.ok
    assert "failed" in r.message
    after, _ = _snapshot(db, pending[0])
    assert after.state is ClipState.PENDING_REVIEW
    assert after.path == before.path
    assert Path(after.path).is_file()


def test_relayout_clips_all_pending_and_dry_run(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    pending: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with session_scope(db) as s:
        s.execute(update(ClipRow).where(ClipRow.id == pending[-1]).values(state=ClipState.APPROVED))
    dry = relayout_clips(pipe_settings, [pending[-1]], all_pending=True, dry_run=True, deps=deps)
    assert {r.clip_id for r in dry if r.ok} == set(pending[:-1])
    assert "would be refused" in capsys.readouterr().out
    done = relayout_clips(pipe_settings, all_pending=True, deps=deps)
    assert {r.clip_id for r in done} == set(pending[:-1])  # the approved clip is not even tried
    assert all(r.ok for r in done)
    out = capsys.readouterr().out
    assert f"{len(done)} of {len(done)} clip(s) re-rendered" in out


def test_wait_for_previews_counts_sent_previews(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, pending: list[int]
) -> None:
    from clipforge.pipeline import wait_for_previews

    results = [relayout_one(pipe_settings, db, cid, deps) for cid in pending[:2]]
    assert wait_for_previews(db, results, timeout_s=0) == 0
    with session_scope(db) as s:
        clip = s.get(ClipRow, pending[0])
        assert clip is not None
        set_flag(s, preview_flag(pending[0]), clip.rendered_at.isoformat())
    assert wait_for_previews(db, results, timeout_s=0) == 1
