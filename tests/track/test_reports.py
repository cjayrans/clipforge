"""D5: weekly digest, monthly report and the kill/scale rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from clipforge import costs
from clipforge.config import Settings
from clipforge.schemas import BrandDecision, SubmissionState
from clipforge.track.reports import PAID_VIEWS_TARGET, kill_scale_status, monthly_report, weekly_report
from tests.support.factories import Factory
from tests.track.helpers import T0, real_post, submission, views

NOW = T0 + timedelta(days=7)  # Sun 2026-11-08 01:10 UTC = Sat 20:10 ET, ISO week 45 locally


@pytest.mark.parametrize(
    ("days", "paid_views", "status"),
    [
        (None, 0, "no data"),
        (0, 0, "too early"),
        (59, 0, "too early"),
        (59, 10**6, "too early"),  # the rule starts at day 60 even for a good month
        (60, PAID_VIEWS_TARGET - 1, "change"),
        (119, 0, "change"),
        (120, PAID_VIEWS_TARGET - 1, "stop"),
        (400, 0, "stop"),
        (60, PAID_VIEWS_TARGET, "scale"),
        (500, 10**7, "scale"),
    ],
)
def test_kill_scale_status(days: int | None, paid_views: float, status: str) -> None:
    got, why = kill_scale_status(days, paid_views)
    assert got == status
    assert why


def test_weekly_report_contents(factory: Factory, session: Session, settings: Settings) -> None:
    ok = real_post(factory, session, published=T0 + timedelta(days=1), hook="best clip", cpm_usd=2.0)
    submission(factory, session, ok, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=10.0)
    views(session, ok, 5000, T0 + timedelta(days=2))
    miss = real_post(factory, session, published=T0 + timedelta(days=2), hook="late clip")
    submission(factory, session, miss, state=SubmissionState.MISSED)
    waiting = real_post(factory, session, published=T0 + timedelta(days=3))
    submission(factory, session, waiting)
    # outside the window, and a dry-run post: neither is counted
    real_post(factory, session, published=T0 - timedelta(days=3))
    real_post(factory, session, published=T0 + timedelta(days=2), url="https://example.invalid/tiktok/1")
    costs.record(
        session, settings.app, "anthropic", "claude-haiku-5-5", 1_000_000, "input_tokens", at=T0 + timedelta(days=1)
    )

    path = weekly_report(session, settings, now=NOW)
    assert path == settings.reports_dir / "weekly-2026-W45.md"
    text = path.read_text(encoding="utf-8")
    assert "# Weekly digest 2026-W45" in text
    assert "Posts live: 3" in text
    assert "1 submitted, 1 missed, 1 unmarked (submission rate 33%)" in text
    assert "Earnings on these posts: $10.00 (est. paid views 5,000)" in text  # $10 at $2 CPM
    assert "API spend: $0.10" in text
    assert "(Test Campaign" in text
    assert "5,000 views - best clip" in text


def test_weekly_report_with_no_data(session: Session, settings: Settings) -> None:
    text = weekly_report(session, settings, now=NOW).read_text(encoding="utf-8")
    assert "Posts live: 0" in text
    assert "submission rate n/a" in text
    assert "- none" in text


def test_weekly_report_is_overwritten_not_appended(session: Session, settings: Settings) -> None:
    first = weekly_report(session, settings, now=NOW)
    second = weekly_report(session, settings, now=NOW + timedelta(hours=1))
    assert first == second
    assert first.read_text(encoding="utf-8").count("# Weekly digest") == 1


def test_weekly_report_default_clock(session: Session, settings: Settings) -> None:
    assert weekly_report(session, settings).exists()


def _month(factory: Factory, session: Session, first_post: datetime, paid: float) -> None:
    anchor = real_post(factory, session, published=first_post)
    submission(factory, session, anchor, state=SubmissionState.SUBMITTED)
    post = real_post(factory, session, published=datetime(2026, 11, 10, 2, 0, tzinfo=UTC))
    submission(factory, session, post, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=paid)


DEC5 = datetime(2026, 12, 5, 13, 0, tzinfo=UTC)  # the first Sunday-ish of December; reports on November


def test_monthly_report_too_early(factory: Factory, session: Session, settings: Settings) -> None:
    _month(factory, session, datetime(2026, 11, 1, 2, 0, tzinfo=UTC), paid=50.0)
    path = monthly_report(session, settings, now=DEC5)
    assert path == settings.reports_dir / "monthly-2026-11.md"
    text = path.read_text(encoding="utf-8")
    assert "# Monthly report 2026-11" in text
    assert "Status: TOO EARLY" in text
    assert "Day 34" in text


def test_monthly_report_change_stop_and_scale(factory: Factory, session: Session, settings: Settings) -> None:
    _month(factory, session, datetime(2026, 9, 1, 2, 0, tzinfo=UTC), paid=50.0)  # day 95 on Dec 5; ~50k paid views
    assert "Status: CHANGE" in monthly_report(session, settings, now=DEC5).read_text(encoding="utf-8")
    later = datetime(2027, 1, 5, 13, 0, tzinfo=UTC)  # day 126; the December report has no paid views
    assert "Status: STOP" in monthly_report(session, settings, now=later).read_text(encoding="utf-8")


def test_monthly_report_scale(factory: Factory, session: Session, settings: Settings) -> None:
    _month(factory, session, datetime(2026, 9, 1, 2, 0, tzinfo=UTC), paid=150.0)  # $150 at $1 CPM = 150k views
    text = monthly_report(session, settings, now=DEC5).read_text(encoding="utf-8")
    assert "Status: SCALE" in text
    assert "150,000 paid views" in text


def test_monthly_report_without_posts(session: Session, settings: Settings) -> None:
    assert "Status: NO DATA" in monthly_report(session, settings, now=DEC5).read_text(encoding="utf-8")
