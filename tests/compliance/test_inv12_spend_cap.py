"""Invariant 12: API spend stops at the monthly cap."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from clipforge import costs
from clipforge.config import AppConfig, Settings
from tests.compliance._helpers import xfail_until

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


@xfail_until("Phase 2: clipforge.pipeline.run_once")
def test_pipeline_pauses_at_cap(settings: Settings, session: Session) -> None:
    from clipforge.pipeline import run_once  # type: ignore[attr-defined]

    _spend(session, settings.app, 15.0)
    session.commit()
    run_once(settings)
    assert costs.is_paused(session) is not None
