"""costs: pricing, ledger, month-to-date in the operator's timezone, pause/resume."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge import costs
from clipforge.config import AppConfig
from clipforge.db import EventRow


def test_price_per_token_and_audio(app_config: AppConfig) -> None:
    assert costs.price_usd(app_config, "claude-haiku-5-5", 1_000_000, "input_tokens") == pytest.approx(0.10)
    assert costs.price_usd(app_config, "claude-sonnet-5-5", 500_000, "output_tokens") == pytest.approx(5.0)
    assert costs.price_usd(app_config, "whisper-large-v3-turbo", 3600, "audio_seconds") == pytest.approx(0.04)


@pytest.mark.parametrize(
    ("model", "kind"),
    [("nope", "input_tokens"), ("claude-haiku-5-5", "furlongs"), ("whisper-large-v3-turbo", "input_tokens")],
)
def test_unknown_price(app_config: AppConfig, model: str, kind: str) -> None:
    with pytest.raises(costs.UnknownPrice):
        costs.price_usd(app_config, model, 1, kind)


def test_record_and_month_to_date_uses_local_month(app_config: AppConfig, session: Session) -> None:
    # 2026-12-01 03:00 UTC is still 30 Nov in New York.
    costs.record(
        session,
        app_config,
        "anthropic",
        "claude-sonnet-5-5",
        100_000,
        "output_tokens",
        at=datetime(2026, 12, 1, 3, 0, tzinfo=UTC),
    )
    costs.record(
        session,
        app_config,
        "anthropic",
        "claude-sonnet-5-5",
        100_000,
        "output_tokens",
        at=datetime(2026, 12, 1, 6, 0, tzinfo=UTC),
    )
    nov = costs.month_to_date(session, app_config, now=datetime(2026, 12, 1, 4, 0, tzinfo=UTC))
    dec = costs.month_to_date(session, app_config, now=datetime(2026, 12, 2, 0, 0, tzinfo=UTC))
    assert nov == pytest.approx(1.0)
    assert dec == pytest.approx(1.0)


def test_pause_resume_events(session: Session) -> None:
    assert costs.is_paused(session) is None
    assert costs.resume(session) is False
    costs.pause(session, "spend cap")
    costs.pause(session, "spend cap")  # idempotent
    assert costs.is_paused(session) == "spend cap"
    assert costs.resume(session) is True
    assert costs.is_paused(session) is None
    events = session.scalars(select(EventRow).where(EventRow.entity == "pipeline")).all()
    assert [e.to_state for e in events] == ["paused", "paused", "running"]
