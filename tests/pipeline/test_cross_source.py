"""moments-v2 nightly run: every source is ranked before anything renders, the best candidates across sources render,
copy is written only for those, stale candidates expire, the per-source cap stops one source only."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clipforge import costs
from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, EventRow, SourceRow, session_scope
from clipforge.pipeline import CANDIDATE_TTL, PipelineDeps, run_once
from clipforge.schemas import ClipState, SourceStatus
from tests.pipeline.test_moment_pipeline import good_signals, groq_json
from tests.rank.anthropic_mock import FakeAnthropic, json_text
from tests.rank.moment_fakes import moment_tools
from tests.support.pipeline_fakes import GROQ_URL, SOURCE_URL, copy_payload
from tests.support.pipeline_fixtures import NIGHT, add_campaign

SECOND_URL = "https://cdn.example.com/gta6/ep2.mp4"


def two_sources_http() -> respx.MockRouter:
    router = respx.mock(assert_all_called=False)
    router.post(GROQ_URL).mock(return_value=httpx.Response(200, json=groq_json()))
    body = b"\x00\x00\x00\x18ftypmp42 fake"
    for url in (SOURCE_URL, SECOND_URL):
        router.get(url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "video/mp4"}))
    router.route(host="api.upload-post.com").mock(return_value=httpx.Response(500))
    return router


def scored(anthropic: FakeAnthropic, best_source: int) -> None:
    def per(cid: str, s: float, e: float, beat: float) -> dict[str, Any]:
        top = cid.startswith(f"{best_source}:")
        return dict.fromkeys(("hook", "arc", "arousal", "sendability", "clarity", "density"), 10 if top else 6)

    anthropic.default = moment_tools(per_candidate=per, other=lambda body: json_text(copy_payload("A hook")))


def test_sources_compete_and_copy_is_only_for_rendered_clips(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, anthropic: FakeAnthropic
) -> None:
    with two_sources_http():
        add_campaign(db, pipe_settings, moment_profile="gta6-streamer", name="First")
        _, second = add_campaign(db, pipe_settings, moment_profile="gta6-streamer", name="Second", url=SECOND_URL)
        scored(anthropic, best_source=second)
        report = run_once(pipe_settings, deps=replace(deps, moment_signals=good_signals()))  # no frames: no vision
    assert report.ranked == 2  # the first source did not fill the night on its own
    assert report.copy_written == report.rendered > 0
    sonnet = [r for r in anthropic.requests if not r.get("tools")]
    assert len(sonnet) == report.copy_written
    with session_scope(db) as s:
        first_clip = s.scalars(select(ClipRow).order_by(ClipRow.id)).first()
        cand = s.get(CandidateRow, first_clip.candidate_id)
        assert cand.source_id == second  # the best candidate overall rendered first
        ranked = [e.id for e in s.scalars(select(EventRow).where(EventRow.entity == "source",
                                                                 EventRow.to_state == "ranked"))]  # fmt: skip
        first_render = s.scalars(select(EventRow).where(EventRow.entity == "clip").order_by(EventRow.id)).first()
        assert max(ranked) < first_render.id  # Phase A (rank all) before Phase B (render)
        unrendered = s.scalars(select(CandidateRow).where(CandidateRow.state == ClipState.CANDIDATE)).all()
        assert all(c.hook is None for c in unrendered)  # no Sonnet spend on candidates nobody reviews
    assert any("source" in line and "spend $" in line for line in report.source_stats)


def test_candidates_expire_three_days_after_ranking(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    clock: Any,
) -> None:
    with two_sources_http():
        add_campaign(db, pipe_settings, moment_profile="gta6-streamer")
        anthropic.default = moment_tools(other=lambda body: json_text(copy_payload("A hook")))
        run_once(pipe_settings, deps=replace(deps, moment_signals=good_signals()))
    with session_scope(db) as s:
        cand = CandidateRow(
            source_id=s.scalars(select(SourceRow)).first().id, start_s=1, end_s=20, score=50, standalone=True,
            brief_ok=True, reasons="old", prompt_version="x", created_at=NIGHT - CANDIDATE_TTL - timedelta(hours=1),
        )  # fmt: skip
        s.add(cand)
        s.flush()
        old_id = cand.id
    clock.now = NIGHT + timedelta(minutes=5)
    report = run_once(pipe_settings, deps=replace(deps, moment_signals=good_signals()))
    assert report.expired_stale >= 1
    with session_scope(db) as s:
        assert s.get(CandidateRow, old_id).state is ClipState.EXPIRED


def test_per_source_cap_fails_that_source_and_the_run_goes_on(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, anthropic: FakeAnthropic
) -> None:
    with two_sources_http():
        _, first = add_campaign(db, pipe_settings, moment_profile="gta6-streamer", name="First")
        _, second = add_campaign(db, pipe_settings, moment_profile="gta6-streamer", name="Second", url=SECOND_URL)
        # the first source already cost its whole cap (e.g. a retried night)
        with session_scope(db) as s, costs.for_source(first, "transcribe"):
            costs.record(s, pipe_settings.app, "groq", "whisper-large-v3-turbo", 3600 * 40, "audio_seconds")
        anthropic.default = moment_tools(other=lambda body: json_text(copy_payload("A hook")))
        report = run_once(pipe_settings, deps=replace(deps, moment_signals=good_signals()))
    assert report.paused is None
    with session_scope(db) as s:
        a, b = s.get(SourceRow, first), s.get(SourceRow, second)
        assert a.status is SourceStatus.FAILED
        assert a.fail_reason == "source_spend_cap"
        assert b.status is SourceStatus.RANKED
        alert = s.scalars(select(EventRow).where(EventRow.to_state == "source_spend_cap")).one()
        assert "per_source_cap_usd" in alert.detail
        tagged = s.scalars(select(costs.CostRow).where(costs.CostRow.source_id == second)).all()
        assert {r.stage for r in tagged} >= {"transcribe", "content", "score"}
