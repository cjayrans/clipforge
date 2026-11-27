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


# --- moments-v2: per-source spend guard ------------------------------------------------------------


def test_for_source_tags_rows_and_caps_one_source(session, app_config) -> None:
    from clipforge.db import CostRow

    with costs.for_source(7, "transcribe"):
        costs.record(session, app_config, "groq", "whisper-large-v3-turbo", 3600, "audio_seconds")
        with costs.stage("vision"):
            costs.record(session, app_config, "anthropic", "claude-haiku-5-5", 1000, "input_tokens")
        costs.guard(session, app_config)  # well under the cap
    costs.record(session, app_config, "anthropic", "claude-haiku-5-5", 1000, "input_tokens")  # untagged
    rows = session.query(CostRow).order_by(CostRow.id).all()
    assert [(r.source_id, r.stage) for r in rows] == [(7, "transcribe"), (7, "vision"), (None, None)]
    assert costs.source_spend(session, 7) == pytest.approx(0.04 + 0.0001)
    assert costs.source_spend(session, 7, stage="vision") == pytest.approx(0.0001)
    with costs.for_source(7, "rank"):
        costs.record(session, app_config, "groq", "whisper-large-v3-turbo", 3600 * 40, "audio_seconds")  # $1.60
        with pytest.raises(costs.SourceSpendCapReached, match="source 7"):
            costs.guard(session, app_config)
    costs.guard(session, app_config)  # other work is not stopped
    with costs.for_source(8, "rank"):
        costs.guard(session, app_config)
