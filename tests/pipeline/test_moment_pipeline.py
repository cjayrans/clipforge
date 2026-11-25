"""Moment selection through the nightly run: a campaign with a moment profile is ranked by the moment ranker, its
candidates store archetype / scores / weights version / gates, and failures degrade instead of stopping the run."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, EventRow, SourceRow, session_scope
from clipforge.pipeline import PipelineDeps, _candidate_model, run_once
from clipforge.rank.media_signals import MediaSignals
from clipforge.rank.profile import SignalRules
from clipforge.review.views import preview_caption
from clipforge.schemas import ClipState, SourceStatus
from tests.rank.anthropic_mock import FakeAnthropic
from tests.rank.moment_fakes import REACTION_SCRIPT, echo_moments, loudness, motion, transcript
from tests.support.pipeline_fakes import GROQ_URL, SOURCE_URL
from tests.support.pipeline_fixtures import add_campaign


def groq_json() -> dict[str, Any]:
    tr = transcript(REACTION_SCRIPT)
    return {
        "text": " ".join(w.text for w in tr.words),
        "language": "en",
        "duration": tr.duration_s,
        "words": [{"word": w.text, "start": w.start_s, "end": w.end_s} for w in tr.words],
        "segments": [{"id": i, "start": s.start_s, "end": s.end_s, "text": s.text} for i, s in enumerate(tr.segments)],
        "x_groq": {"id": "req_test"},
    }


@pytest.fixture
def reaction_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.post(GROQ_URL).mock(return_value=httpx.Response(200, json=groq_json()))
        router.get(SOURCE_URL).mock(
            return_value=httpx.Response(
                200, content=b"\x00\x00\x00\x18ftypmp42 fake", headers={"content-type": "video/mp4"}
            )
        )
        router.route(host="api.upload-post.com").mock(return_value=httpx.Response(500))
        yield router


class Signals:
    def __init__(self, result: MediaSignals) -> None:
        self.result = result
        self.calls: list[tuple[Path, SignalRules]] = []

    def __call__(self, path: Path, rules: SignalRules) -> MediaSignals:
        self.calls.append((path, rules))
        return self.result


def good_signals() -> Signals:
    return Signals(
        MediaSignals(loudness_db=loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0}), motion=motion(spikes={30.0: 25.0}))
    )


def test_moment_campaign_stores_calibration_fields(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    reaction_http: respx.MockRouter,
) -> None:
    signals = good_signals()
    add_campaign(db, pipe_settings, moment_profile="gta6-streamer", copy_style_notes="Meme tone.")
    report = run_once(pipe_settings, deps=replace(deps, moment_signals=signals))
    assert report.candidates_created == 2
    assert len(signals.calls) == 1
    assert signals.calls[0][1].loud_z == 2.0
    tools = [r["tools"][0]["name"] for r in anthropic.requests if r.get("tools")]
    assert tools == ["score_moments"]  # the transcript-window scorer is not used for this campaign
    with session_scope(db) as s:
        rows = s.scalars(select(CandidateRow).order_by(CandidateRow.id)).all()
        assert [r.archetype for r in rows] == ["blunder", "blunder"]
        for r in rows:
            assert r.moment_profile == "gta6-streamer"
            assert r.weights_version == "gta6-streamer-v1"
            assert (r.score_reaction, r.score_event, r.score_setup, r.score_absurdity) == (8, 7, 7, 8)
            assert r.score == round(10 * (0.35 * 8 + 0.25 * 7 + 0.25 * 7 + 0.15 * 8))
            names = [g["name"] for g in r.gate_results_json]
            assert names == ["trigger_reaction", "dead_footage", "brief", "skip_rules", "min_score"]
            assert r.skip_flags == []
            assert r.review_flags == []
            assert r.prompt_version == "score_moments@1+clip_copy@3"
            assert 12 <= r.end_s - r.start_s <= 45
            model = _candidate_model(r)  # what a "New hook" re-render hands the copywriter
            assert model.moment is not None
            assert model.moment.reason.startswith("Sells his car")
        src = s.scalars(select(SourceRow)).one()
        assert src.status is SourceStatus.RANKED
        ranked = s.scalars(select(EventRow).where(EventRow.entity == "source", EventRow.to_state == "ranked")).one()
        assert "moments (gta6-streamer-v1)" in (ranked.detail or "")
        clip = s.scalars(select(ClipRow).order_by(ClipRow.id)).first()
        assert clip is not None
        assert clip.state is ClipState.PENDING_REVIEW
        assert "Moment: blunder | reaction 8" in preview_caption(s, pipe_settings, clip)


def test_failed_media_signals_still_rank_on_the_transcript(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, reaction_http: respx.MockRouter
) -> None:
    signals = Signals(MediaSignals(problems=["loudness: ffmpeg failed (exit 1)", "motion: ffmpeg failed (exit 1)"]))
    add_campaign(db, pipe_settings, moment_profile="gta6-streamer")
    report = run_once(pipe_settings, deps=replace(deps, moment_signals=signals))
    assert report.candidates_created >= 1
    assert any("moment signals" in p and "loudness" in p for p in report.problems)
    with session_scope(db) as s:
        row = s.scalars(select(CandidateRow)).first()
        assert row is not None
        dead = next(g for g in row.gate_results_json if g["name"] == "dead_footage")
        assert dead["passed"] is None
        assert "no loudness data" in dead["detail"]


def test_missing_profile_alerts_and_leaves_the_source_for_next_night(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    reaction_http: respx.MockRouter,
) -> None:
    add_campaign(db, pipe_settings, moment_profile="no-such-profile")  # upsert skips the `cf campaign add` check
    report = run_once(pipe_settings, deps=replace(deps, moment_signals=good_signals()))
    assert report.candidates_created == 0
    assert not [r for r in anthropic.requests if r.get("tools")]
    with session_scope(db) as s:
        assert s.scalars(select(SourceRow)).one().status is SourceStatus.TRANSCRIBED
        alerts = s.scalars(select(EventRow).where(EventRow.entity == "alert")).all()
        assert any("moment profile not found" in (a.detail or "") for a in alerts)


def test_campaign_without_profile_keeps_the_window_ranker(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    reaction_http: respx.MockRouter,
) -> None:
    signals = good_signals()
    add_campaign(db, pipe_settings)
    run_once(pipe_settings, deps=replace(deps, moment_signals=signals))
    assert signals.calls == []
    assert {r["tools"][0]["name"] for r in anthropic.requests if r.get("tools")} == {"score_windows"}
    with session_scope(db) as s:
        assert all(r.archetype is None for r in s.scalars(select(CandidateRow)))


def test_flagged_clip_still_waits_for_review_with_a_manual_check_line(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    reaction_http: respx.MockRouter,
) -> None:
    default = anthropic.default
    assert default is not None
    flagged = echo_moments(lambda cid, s, e, t: {"review_flags": ["sexual"]})

    def respond(body: dict[str, Any]) -> Any:
        if body.get("tools") and body["tools"][0]["name"] == "score_moments":
            return flagged(body)
        return default(body)

    anthropic.default = respond
    add_campaign(db, pipe_settings, moment_profile="gta6-streamer")
    report = run_once(pipe_settings, deps=replace(deps, moment_signals=good_signals()))
    assert report.ready >= 1
    with session_scope(db) as s:
        clips = s.scalars(select(ClipRow).order_by(ClipRow.id)).all()
        assert clips
        for clip in clips:
            assert clip.state is ClipState.PENDING_REVIEW  # flagged, not rejected, and not approved
            assert "CHECK BEFORE APPROVING: flagged sexual" in preview_caption(s, pipe_settings, clip)
        assert all(r.review_flags == ["sexual"] for r in s.scalars(select(CandidateRow)))
