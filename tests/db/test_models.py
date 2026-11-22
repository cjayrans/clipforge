"""DB models: constraints, UTC datetimes, round trips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from clipforge.db import CampaignRow, CostRow, SourceRow
from clipforge.schemas import CampaignStatus, Platform, SourceStatus
from tests.support.factories import Factory


def test_campaign_round_trip(factory: Factory, session: Session) -> None:
    c = factory.campaign(required_tags=["#DylanPage"], cpm_by_platform={"tiktok": 1.2})
    session.expire_all()
    loaded = session.get(CampaignRow, c.id)
    assert loaded is not None
    assert loaded.status is CampaignStatus.ACTIVE
    assert loaded.required_tags == ["#DylanPage"]
    assert loaded.cpm_by_platform == {"tiktok": 1.2}
    assert loaded.created_at.tzinfo is UTC


def test_datetimes_come_back_aware_utc(session: Session) -> None:
    est = timezone(timedelta(hours=-5))
    session.add(
        CostRow(at=datetime(2026, 11, 22, 20, 0, tzinfo=est), service="s", model="m", units=1, unit_kind="u", usd=0)
    )
    session.flush()
    session.expire_all()
    row = session.scalars(select(CostRow)).one()
    assert row.at == datetime(2026, 11, 23, 1, 0, tzinfo=UTC)


def test_naive_datetime_refused(session: Session) -> None:
    session.add(CostRow(at=datetime(2026, 11, 22, 20, 0), service="s", model="m", units=1, unit_kind="u", usd=0))
    with pytest.raises(StatementError, match="naive"):
        session.flush()


def test_source_url_unique_per_campaign(factory: Factory, session: Session) -> None:
    c = factory.campaign()
    factory.source(c, url="https://cdn.example.com/a.mp4")
    factory.source(factory.campaign(), url="https://cdn.example.com/a.mp4")  # other campaign: fine
    with pytest.raises(IntegrityError):
        factory.source(c, url="https://cdn.example.com/a.mp4")


def test_enum_check_constraint(factory: Factory, session: Session) -> None:
    src = factory.source()
    assert src.status is SourceStatus.REGISTERED
    session.add(SourceRow(campaign_id=src.campaign_id, url="https://x/b.mp4", status="bogus"))
    with pytest.raises(StatementError):
        session.flush()


def test_foreign_keys_enforced(session: Session) -> None:
    session.add(SourceRow(campaign_id=9999, url="https://cdn.example.com/a.mp4"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_post_platform_enum(factory: Factory) -> None:
    post = factory.post(platform=Platform.INSTAGRAM)
    assert post.platform is Platform.INSTAGRAM
