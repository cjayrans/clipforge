"""Invariant 12: API spend stops at the monthly cap."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge import costs
from clipforge.config import AppConfig, Settings
from clipforge.db import EventRow, make_engine, make_session_factory
from clipforge.db.migrate import upgrade

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
    from clipforge.pipeline import run_once

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
