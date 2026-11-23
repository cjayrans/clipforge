"""Weekly digest and monthly report (PRD §4.5). Markdown files in `data/reports/`; the scheduler also sends them.

Weekly (Saturday 08:00): posts, submission rate, brand decisions, earnings, API costs, top/bottom clips.
Monthly (first Sunday 08:00): the same for the month plus the kill/scale rule status:
  day >= 60 and < 100k paid views in the month -> CHANGE; day >= 120 and still below -> STOP; at/above -> SCALE.
Day 0 is the first real (non-dry-run) post. "Paid views" are an ESTIMATE: paid_usd / CPM x 1000 per platform, because
Content Rewards has no API for views.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CampaignRow, ClipRow, CostRow, SubmissionRow
from clipforge.publish.queries import lineage
from clipforge.schemas import SubmissionState
from clipforge.track.metrics import estimated_paid_views, iso_week, latest_views, live_real_posts

PAID_VIEWS_TARGET = 100_000
CHANGE_DAY = 60
STOP_DAY = 120


def kill_scale_status(days_since_start: int | None, paid_views_month: float) -> tuple[str, str]:
    """(status, explanation): too early / scale / change / stop. Pure."""
    if days_since_start is None:
        return "no data", "No real post has gone live yet."
    if days_since_start < CHANGE_DAY:
        return "too early", f"Day {days_since_start}: the rule applies from day {CHANGE_DAY}."
    if paid_views_month >= PAID_VIEWS_TARGET:
        return (
            "scale",
            f"Day {days_since_start}: {paid_views_month:,.0f} paid views reached the {PAID_VIEWS_TARGET:,} target.",
        )
    if days_since_start >= STOP_DAY:
        return (
            "stop",
            f"Day {days_since_start}: under {PAID_VIEWS_TARGET:,} paid views a month at day {STOP_DAY}+: stop.",
        )
    return "change", f"Day {days_since_start}: under {PAID_VIEWS_TARGET:,} paid views a month: change the approach."


@dataclass
class ClipLine:
    clip_id: int
    campaign: str
    platform: str
    views: int
    hook: str


@dataclass
class Stats:
    label: str
    start: datetime
    end: datetime
    posts_live: int = 0
    submissions: int = 0
    submitted: int = 0
    missed: int = 0
    awaiting: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    earnings_usd: float = 0.0
    paid_views_est: float = 0.0
    api_cost_usd: float = 0.0
    top: list[ClipLine] = field(default_factory=list)
    bottom: list[ClipLine] = field(default_factory=list)
    days_since_start: int | None = None

    @property
    def submission_rate(self) -> float | None:
        return self.submitted / self.submissions if self.submissions else None


def collect_stats(session: Session, label: str, start: datetime, end: datetime) -> Stats:
    stats = Stats(label=label, start=start, end=end)
    clips: list[ClipLine] = []
    for post in live_real_posts(session):
        if post.published_at is None or not start <= post.published_at < end:
            continue
        stats.posts_live += 1
        sub = session.scalar(select(SubmissionRow).where(SubmissionRow.post_id == post.id))
        if sub is not None:
            stats.submissions += 1
            stats.submitted += int(sub.state is SubmissionState.SUBMITTED)
            stats.missed += int(sub.state is SubmissionState.MISSED)
            stats.awaiting += int(sub.state is SubmissionState.AWAITING)
            stats.earnings_usd += sub.paid_usd
            stats.paid_views_est += estimated_paid_views(session, sub)
        views = latest_views(session, post.id)
        if views is not None:
            clip = session.get(ClipRow, post.clip_id)
            if clip is not None:
                lin = lineage(session, clip)
                clips.append(ClipLine(clip.id, lin.campaign.name, post.platform.value, views, lin.candidate.hook or ""))
    for decision in session.scalars(
        select(SubmissionRow.brand_decision).where(
            SubmissionRow.decided_at.is_not(None), SubmissionRow.decided_at >= start, SubmissionRow.decided_at < end
        )
    ):
        stats.decisions[decision.value] = stats.decisions.get(decision.value, 0) + 1
    stats.api_cost_usd = float(
        session.scalar(select(func.coalesce(func.sum(CostRow.usd), 0.0)).where(CostRow.at >= start, CostRow.at < end))
        or 0.0
    )
    clips.sort(key=lambda c: c.views, reverse=True)
    stats.top = clips[:3]
    stats.bottom = list(reversed(clips[-3:])) if len(clips) > 3 else []
    return stats


def _lines(items: list[ClipLine]) -> list[str]:
    return [f"- clip {c.clip_id} ({c.campaign}, {c.platform}): {c.views:,} views - {c.hook}" for c in items] or [
        "- none"
    ]


def render(stats: Stats, title: str, extra: list[str] | None = None) -> str:
    rate = "n/a" if stats.submission_rate is None else f"{stats.submission_rate:.0%}"
    decisions = ", ".join(f"{k} {v}" for k, v in sorted(stats.decisions.items())) or "none"
    out = [
        f"# {title}",
        f"Period: {stats.start:%Y-%m-%d} to {stats.end:%Y-%m-%d} (UTC)",
        "",
        f"- Posts live: {stats.posts_live}",
        f"- Submissions: {stats.submitted} submitted, {stats.missed} missed, {stats.awaiting} unmarked "
        f"(submission rate {rate})",
        f"- Brand decisions this period: {decisions}",
        f"- Earnings on these posts: ${stats.earnings_usd:.2f} (est. paid views {stats.paid_views_est:,.0f})",
        f"- API spend: ${stats.api_cost_usd:.2f}",
        "",
        "## Top clips",
        *_lines(stats.top),
        "",
        "## Bottom clips",
        *_lines(stats.bottom),
    ]
    if extra:
        out += ["", *extra]
    return "\n".join(out) + "\n"


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def weekly_report(session: Session, settings: Settings, *, now: datetime | None = None) -> Path:
    """`cf report weekly`: write the digest for the last 7 days and return its path."""
    end = _now(now)
    stats = collect_stats(session, "weekly", end - timedelta(days=7), end)
    path = settings.reports_dir / f"weekly-{iso_week(end, settings.app.tz)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(stats, f"Weekly digest {iso_week(end, settings.app.tz)}"), encoding="utf-8")
    return path


def first_real_post_at(session: Session) -> datetime | None:
    times = [p.published_at for p in live_real_posts(session) if p.published_at is not None]
    return min(times) if times else None


def monthly_report(session: Session, settings: Settings, *, now: datetime | None = None) -> Path:
    """Report for the previous calendar month (in the operator's timezone) with the kill/scale status."""
    end_utc = _now(now)
    tz = settings.app.tz
    local_now = end_utc.astimezone(tz)
    this_month = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month = (this_month - timedelta(days=1)).replace(day=1)
    stats = collect_stats(session, "monthly", prev_month.astimezone(UTC), this_month.astimezone(UTC))
    first = first_real_post_at(session)
    days = (end_utc - first).days if first is not None else None
    stats.days_since_start = days
    status, why = kill_scale_status(days, stats.paid_views_est)
    name = f"{prev_month:%Y-%m}"
    body = render(
        stats,
        f"Monthly report {name}",
        [
            "## Kill / scale rule",
            f"- Status: {status.upper()}",
            f"- {why}",
            f"- Campaigns active now: {_active_campaigns(session)}",
        ],
    )
    path = settings.reports_dir / f"monthly-{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _active_campaigns(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(CampaignRow).where(CampaignRow.status == "active")) or 0)
