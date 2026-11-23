"""Builders for tracking tests: real (non-dry-run) live posts with submissions and view snapshots."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from clipforge.db import CampaignRow, PostRow, SubmissionRow, ViewSnapshotRow, transition
from clipforge.schemas import BrandDecision, Platform, PostState, SubmissionState
from tests.support.factories import Factory

_n = itertools.count(1)
T0 = datetime(2026, 11, 1, 1, 10, tzinfo=UTC)


def real_post(
    factory: Factory,
    session: Session,
    *,
    published: datetime = T0,
    platform: Platform = Platform.TIKTOK,
    campaign: CampaignRow | None = None,
    hook: str = "the hook",
    url: str | None = None,
    **campaign_kw: Any,
) -> PostRow:
    camp = campaign or factory.campaign(**campaign_kw)
    candidate = factory.candidate(factory.source(camp), hook=hook, start_s=10.0, end_s=40.0)
    clip = factory.clip(candidate)
    post = factory.post(clip, platform)
    transition(session, post, PostState.PUBLISHING)
    post.post_url = url or f"https://www.tiktok.com/@u/video/{next(_n)}"
    post.published_at = published
    transition(session, post, PostState.LIVE)
    return post


def submission(
    factory: Factory,
    session: Session,
    post: PostRow,
    *,
    state: SubmissionState = SubmissionState.AWAITING,
    brand: BrandDecision = BrandDecision.PENDING,
    paid: float = 0.0,
    submitted_at: datetime | None = None,
) -> SubmissionRow:
    sub = factory.submission(post, deadline_at=(post.published_at or T0) + timedelta(minutes=30))
    if state is not SubmissionState.AWAITING:
        sub.submitted_at = submitted_at or (post.published_at or T0) + timedelta(minutes=10)
        transition(session, sub, state)
    sub.brand_decision = brand
    sub.paid_usd = paid
    session.flush()
    return sub


def views(session: Session, post: PostRow, count: int, at: datetime, source: str = "manual") -> ViewSnapshotRow:
    row = ViewSnapshotRow(post_id=post.id, taken_at=at, views=count, source=source)
    session.add(row)
    session.flush()
    return row
