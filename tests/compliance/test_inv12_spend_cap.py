"""Invariant 12: API spend stops at the monthly cap."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from clipforge import costs
from clipforge.config import AppConfig, Settings
from clipforge.db import ClipRow, CostRow, EventRow, make_engine, make_session_factory, session_scope
from clipforge.db.migrate import upgrade
from clipforge.pipeline import PipelineDeps, run_once
from clipforge.schemas import ClipState
from tests.rank.anthropic_mock import FakeAnthropic
from tests.support.pipeline_fixtures import add_campaign

NOW = datetime(2026, 11, 22, 18, 0, tzinfo=UTC)


def _spend(session: Session, app: AppConfig, usd: float) -> None:
    # Sonnet output: $10 per million tokens.
    costs.record(session, app, "anthropic", "claude-sonnet-5-5", usd * 100_000, "output_tokens", at=NOW)


def test_spend_cap_pauses_pipeline(app_config: AppConfig, session: Session) -> None:
    _spend(session, app_config, 14.99)
    assert costs.guard(session, app_config, now=NOW) == pytest.approx(14.99)
    _spend(session, app_config, 0.01)
    with pytest.raises(costs.SpendCapReached):
        costs.guard(session, app_config, now=NOW)


def test_pause_blocks_until_resume(app_config: AppConfig, session: Session) -> None:
    costs.pause(session, "spend cap reached")
    with pytest.raises(costs.SpendCapReached, match="paused"):
        costs.guard(session, app_config, now=NOW)
    costs.resume(session)
    costs.guard(session, app_config, now=NOW)


def test_resume_does_not_bypass_cap(app_config: AppConfig, session: Session) -> None:
    _spend(session, app_config, 20.0)
    costs.pause(session, "spend cap reached")
    costs.resume(session)
    with pytest.raises(costs.SpendCapReached, match="reached the cap"):
        costs.guard(session, app_config, now=NOW)


def test_pipeline_pauses_at_cap(settings: Settings) -> None:
    """At the cap, the nightly run pauses before any paid stage and records the pause once (the bot delivers it)."""
    # Phase 2: the session must use the database run_once opens (the generic `session` fixture is a separate file).
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    session = make_session_factory(engine)()
    try:
        _spend(session, settings.app, 15.0)
        session.commit()
        report = run_once(settings)
        assert report.paused is not None
        assert costs.is_paused(session) is not None
        run_once(settings)  # still paused: no second pause event, so no second alert
        pauses = session.scalars(select(EventRow).where(EventRow.entity == "pipeline", EventRow.to_state == "paused"))
        assert len(list(pauses)) == 1
    finally:
        session.close()
        engine.dispose()


def test_new_hook_spends_nothing_while_paused(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    mocked_http: respx.MockRouter,
) -> None:
    """Phase 2 review: the bot's New hook (a paid Sonnet call) is blocked by the pause like every other paid call.
    The clip returns to review with a newer render time, so the approval given before the tap no longer counts."""
    from clipforge.compliance import ComplianceError, assert_human_approved
    from clipforge.pipeline import begin_rerender, finish_rerender
    from clipforge.publish.queries import approvals_for
    from clipforge.review.service import approve_clip, render_stamp

    add_campaign(db, pipe_settings)
    assert run_once(pipe_settings, deps=deps).ready >= 1
    approved_at = datetime(2026, 11, 24, 0, 50, tzinfo=UTC)
    with session_scope(db) as s:
        clip = s.scalars(select(ClipRow).where(ClipRow.state == ClipState.PENDING_REVIEW)).first()
        assert clip is not None
        clip_id = clip.id
        approve_clip(s, clip_id, stamp=render_stamp(clip.rendered_at), now=approved_at)
        costs.pause(s, "monthly cap")
        cost_rows = s.scalar(select(func.count()).select_from(CostRow))
    requests = len(anthropic.requests)

    job = begin_rerender(pipe_settings, db, clip_id, "new_hook", approved_at)
    message = finish_rerender(pipe_settings, db, job, deps)

    assert "failed" in message
    assert len(anthropic.requests) == requests
    with session_scope(db) as s:
        assert s.scalar(select(func.count()).select_from(CostRow)) == cost_rows
        clip = s.get(ClipRow, clip_id)
        assert clip is not None
        assert clip.state is ClipState.PENDING_REVIEW
        with pytest.raises(ComplianceError):
            assert_human_approved(approvals_for(s, clip_id), clip.rendered_at)
