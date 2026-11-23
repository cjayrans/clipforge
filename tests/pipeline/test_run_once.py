"""P2.2: nightly orchestration rules (early stop, isolation, spend cap, render retry, sweeps)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from clipforge import costs
from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, CostRow, EventRow, SourceRow, session_scope
from clipforge.ingest.registry import set_campaign_status
from clipforge.pipeline import PipelineDeps, begin_rerender, run_once
from clipforge.schemas import CampaignStatus, ClipState, SourceStatus
from tests.rank.anthropic_mock import FakeAnthropic
from tests.support.pipeline_fakes import FakeRender
from tests.support.pipeline_fixtures import Clock, add_campaign


def with_app(settings: Settings, section: str | None = None, **values: Any) -> Settings:
    app = settings.app
    if section is None:
        app = app.model_copy(update=values)
    else:
        app = app.model_copy(update={section: getattr(app, section).model_copy(update=values)})
    return Settings(app=app, secrets=settings.secrets, env=settings.env)


def alert_kinds(db: sessionmaker[Session]) -> list[str | None]:
    with session_scope(db) as s:
        return [e.to_state for e in s.scalars(select(EventRow).where(EventRow.entity == "alert").order_by(EventRow.id))]


def states(db: sessionmaker[Session], model: type[CandidateRow] | type[ClipRow]) -> list[ClipState]:
    with session_scope(db) as s:
        return [r.state for r in s.scalars(select(model).order_by(model.id))]


def test_nothing_to_do_alerts_once_and_spends_nothing(pipe_settings: Settings, db: sessionmaker[Session]) -> None:
    report = run_once(pipe_settings, deps=PipelineDeps(local_transcriber=None))
    assert report.ready == 0
    assert alert_kinds(db) == ["pipeline_empty"]
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(CostRow)) == 0
        done = s.scalars(select(EventRow).where(EventRow.entity == "pipeline")).one()
        assert done.to_state == "done"


def test_early_stop_then_backlog_first_next_night(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    mocked_http: respx.MockRouter,
    clock: Clock,
) -> None:
    settings = with_app(pipe_settings, "pipeline", nightly_candidate_target=1)
    add_campaign(db, settings)
    first = run_once(settings, deps=deps)
    assert first.ready == 1
    assert first.candidates_created > 1, "the talk fixture should give more than one candidate"
    assert first.stopped_early
    assert states(db, CandidateRow).count(ClipState.CANDIDATE) == first.candidates_created - 1

    calls = len(anthropic.requests)
    clock.now += timedelta(days=1)
    second = run_once(settings, deps=deps)
    assert second.ready == 1  # rendered from the backlog
    assert second.sources_seen == 0
    assert len(anthropic.requests) == calls  # the backlog costs no API money


def test_one_failing_source_does_not_stop_the_next(
    pipe_settings: Settings, db: sessionmaker[Session], deps: PipelineDeps, mocked_http: respx.MockRouter
) -> None:
    bad_url = "https://cdn.example.com/gta6/missing.mp4"
    mocked_http.get(bad_url).mock(return_value=httpx.Response(404))
    _, bad = add_campaign(db, pipe_settings, url=bad_url, name="Broken Link Campaign", priority=5)
    _, good = add_campaign(db, pipe_settings)
    report = run_once(pipe_settings, deps=deps)
    with session_scope(db) as s:
        bad_row = s.get(SourceRow, bad)
        good_row = s.get(SourceRow, good)
        assert bad_row is not None
        assert good_row is not None
        assert bad_row.status is SourceStatus.REGISTERED  # retried next night (CR-A5)
        assert good_row.status is SourceStatus.RANKED
    assert report.ready >= 1
    assert any("download" in p for p in report.problems)
    assert "download_failed" in alert_kinds(db)
    assert "pipeline_empty" not in alert_kinds(db)


def test_spend_cap_mid_run_pauses_and_keeps_the_cost_rows(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    mocked_http: respx.MockRouter,
) -> None:
    """Groq (90 s, about $0.001) pushes spend over a tiny cap: ranking never calls Anthropic, the run pauses."""
    settings = with_app(pipe_settings, monthly_api_cap_usd=0.0009)
    _, source_id = add_campaign(db, settings)
    report = run_once(settings, deps=deps)
    assert report.paused is not None
    assert anthropic.requests == []
    with session_scope(db) as s:
        src = s.get(SourceRow, source_id)
        assert src is not None
        assert src.status is SourceStatus.TRANSCRIBED
        assert [r.service for r in s.scalars(select(CostRow))] == ["groq"]  # committed through the ledger
        assert costs.is_paused(s) is not None
    assert "pipeline_empty" not in alert_kinds(db)  # the pause itself is the alert


def test_render_failure_retries_once_then_expires(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    mocked_http: respx.MockRouter,
    render: FakeRender,
    clock: Clock,
) -> None:
    render.fail = 1000
    add_campaign(db, pipe_settings)
    first = run_once(pipe_settings, deps=deps)
    assert first.ready == 0
    assert set(states(db, CandidateRow)) == {ClipState.CANDIDATE}
    clock.now += timedelta(days=1)
    run_once(pipe_settings, deps=deps)
    assert set(states(db, CandidateRow)) == {ClipState.EXPIRED}
    assert alert_kinds(db).count("render_failed") == 2 * first.candidates_created


def test_qa_failure_never_reaches_review(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    mocked_http: respx.MockRouter,
    render: FakeRender,
) -> None:
    render.out_fps = 25.0  # QA requires the configured 30 fps
    add_campaign(db, pipe_settings)
    report = run_once(pipe_settings, deps=deps)
    assert report.ready == 0
    assert report.qa_failed == report.rendered > 0
    assert set(states(db, ClipRow)) == {ClipState.QA_FAILED}
    with session_scope(db) as s:
        ev = s.scalars(select(EventRow).where(EventRow.to_state == "qa_failed")).first()
        assert ev is not None
        assert "fps" in ev.detail


def test_nightly_sweep_expires_drained_campaigns_and_recovers_stranded_clips(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    mocked_http: respx.MockRouter,
    clock: Clock,
) -> None:
    add_campaign(db, pipe_settings)
    run_once(pipe_settings, deps=deps)
    with session_scope(db) as s:
        first_clip = s.scalars(select(ClipRow).order_by(ClipRow.id)).first()
        assert first_clip is not None
        clip_id, rendered_at = first_clip.id, first_clip.rendered_at
    # A re-render started in the bot, then the bot died: the clip is left RENDERED.
    begin_rerender(pipe_settings, db, clip_id, "new_hook", clock.now)
    clock.now += timedelta(hours=2)
    report = run_once(pipe_settings, deps=deps)
    assert report.recovered == 1
    with session_scope(db) as s:
        clip = s.get(ClipRow, clip_id)
        assert clip is not None
        assert clip.state is ClipState.PENDING_REVIEW
        assert clip.rendered_at > rendered_at  # any earlier approval is stale
    assert "rerender_failed" in alert_kinds(db)

    # Draining the campaign outside the CLI (e.g. status edited) is caught by the nightly sweep too.
    with session_scope(db) as s:
        camp_id = s.scalars(select(SourceRow.campaign_id)).first()
        assert camp_id is not None
        set_campaign_status(s, pipe_settings, camp_id, CampaignStatus.DRAINED)
    assert set(states(db, ClipRow)) == {ClipState.EXPIRED}
    assert run_once(pipe_settings, deps=deps).expired == 0  # already expired by the command
