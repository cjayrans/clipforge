"""Shared read helpers for tracking: views per post, estimated paid views, week keys. No state changes."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import CampaignRow, PostRow, SubmissionRow, ViewSnapshotRow
from clipforge.publish.queries import campaign_model
from clipforge.schemas import PostState


def is_fake_url(url: str | None) -> bool:
    """dry_run posts carry `https://example.invalid/...`; they never count as real output."""
    return url is not None and (urlsplit(url).hostname or "").endswith(".invalid")


def latest_views(session: Session, post_id: int) -> int | None:
    row = session.scalars(
        select(ViewSnapshotRow)
        .where(ViewSnapshotRow.post_id == post_id)
        .order_by(ViewSnapshotRow.taken_at.desc(), ViewSnapshotRow.id.desc())
    ).first()
    return row.views if row is not None else None


def views_within(session: Session, post: PostRow, days: int = 7) -> int | None:
    """The highest view count recorded within `days` of publishing (counts only grow), or None without data."""
    if post.published_at is None:
        return None
    cutoff = post.published_at + timedelta(days=days)
    rows = session.scalars(
        select(ViewSnapshotRow.views).where(ViewSnapshotRow.post_id == post.id, ViewSnapshotRow.taken_at <= cutoff)
    ).all()
    return max(rows) if rows else None


def iso_week(at: datetime, tz: ZoneInfo) -> str:
    year, week, _ = at.astimezone(tz).isocalendar()
    return f"{year}-W{week:02d}"


def estimated_paid_views(session: Session, sub: SubmissionRow) -> float:
    """paid_usd / CPM x 1000 for the platform of the post. An estimate: Content Rewards has no API for views."""
    if sub.paid_usd <= 0:
        return 0.0
    post = session.get(PostRow, sub.post_id)
    campaign = session.get(CampaignRow, sub.campaign_id)
    if post is None or campaign is None:
        return 0.0
    cpm = campaign_model(campaign).cpm_for(post.platform)
    return sub.paid_usd / cpm * 1000 if cpm > 0 else 0.0


def min_payout_views(campaign: CampaignRow, post: PostRow) -> float | None:
    """Views needed to reach the campaign's minimum payout on this platform (None when the rate is unknown)."""
    model = campaign_model(campaign)
    cpm = model.cpm_for(post.platform)
    if cpm <= 0:
        return None
    return model.min_payout_for(post.platform) / cpm * 1000


def live_real_posts(session: Session) -> list[PostRow]:
    posts = session.scalars(select(PostRow).where(PostRow.state == PostState.LIVE).order_by(PostRow.id)).all()
    return [p for p in posts if not is_fake_url(p.post_url)]
