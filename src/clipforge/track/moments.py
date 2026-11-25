"""Moment-ranker calibration: 24 h / 72 h views per post, and per-archetype performance (`cf report moments`).

`posts.views_24h` / `views_72h` are filled from `view_snapshots` (YouTube poller, reconcile imports) once the mark
has passed: the snapshot nearest the mark, if one was taken within MARK_TOLERANCE of it. A value already stored is
never overwritten, so a hand-entered number wins. Only live posts with a real URL count (dry-run posts never do).

Writing a view number is not a state change, so no `events` row is written.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, PostRow, ViewSnapshotRow, utcnow
from clipforge.schemas import PostState
from clipforge.track.metrics import is_fake_url

MARKS: dict[str, timedelta] = {"views_24h": timedelta(hours=24), "views_72h": timedelta(hours=72)}
MARK_TOLERANCE = timedelta(hours=6)


def _nearest_views(session: Session, post: PostRow, mark: datetime) -> int | None:
    rows = session.execute(
        select(ViewSnapshotRow.taken_at, ViewSnapshotRow.views).where(
            ViewSnapshotRow.post_id == post.id,
            ViewSnapshotRow.taken_at >= mark - MARK_TOLERANCE,
            ViewSnapshotRow.taken_at <= mark + MARK_TOLERANCE,
        )
    ).all()
    if not rows:
        return None
    _, views = min(rows, key=lambda r: (abs((r[0] - mark).total_seconds()), r[0]))
    return int(views)


def fill_view_marks(session: Session, now: datetime | None = None) -> int:
    """Fill missing views_24h / views_72h of live posts whose mark has passed. Returns how many values were set."""
    now = now or utcnow()
    filled = 0
    posts = session.scalars(
        select(PostRow).where(PostRow.state == PostState.LIVE, PostRow.published_at.is_not(None))
    ).all()
    for post in posts:
        if is_fake_url(post.post_url) or post.published_at is None:
            continue
        for column, delta in MARKS.items():
            mark = post.published_at + delta
            if getattr(post, column) is not None or now < mark:
                continue
            views = _nearest_views(session, post, mark)
            if views is not None:
                setattr(post, column, views)
                filled += 1
    session.flush()
    return filled


@dataclass
class ArchetypeStats:
    archetype: str
    posts: int = 0
    weights: set[str] = field(default_factory=set)
    scores: list[int] = field(default_factory=list)
    views_24h: list[int] = field(default_factory=list)
    views_72h: list[int] = field(default_factory=list)
    by_platform_72h: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))


def _median(values: list[int]) -> str:
    return f"{statistics.median(values):,.0f}" if values else "-"


def _mean(values: list[int]) -> str:
    return f"{statistics.fmean(values):,.0f}" if values else "-"


def archetype_stats(session: Session) -> list[ArchetypeStats]:
    rows = session.execute(
        select(PostRow, CandidateRow)
        .join(ClipRow, ClipRow.id == PostRow.clip_id)
        .join(CandidateRow, CandidateRow.id == ClipRow.candidate_id)
        .where(PostRow.state == PostState.LIVE, CandidateRow.archetype.is_not(None))
    ).all()
    stats: dict[str, ArchetypeStats] = {}
    for post, cand in rows:
        if is_fake_url(post.post_url):
            continue
        assert cand.archetype is not None
        st = stats.setdefault(cand.archetype, ArchetypeStats(cand.archetype))
        st.posts += 1
        st.scores.append(cand.score)
        if cand.weights_version:
            st.weights.add(cand.weights_version)
        if post.views_24h is not None:
            st.views_24h.append(post.views_24h)
        if post.views_72h is not None:
            st.views_72h.append(post.views_72h)
            st.by_platform_72h[post.platform.value].append(post.views_72h)
    return sorted(stats.values(), key=lambda s: (-(statistics.median(s.views_72h) if s.views_72h else -1), s.archetype))


def render_report(stats: list[ArchetypeStats]) -> str:
    if not stats:
        return "No live posts from moment-ranked clips yet."
    if not any(s.views_24h or s.views_72h for s in stats):
        total = sum(s.posts for s in stats)
        return (
            f"{total} live post(s) from moment-ranked clips, but no 24 h / 72 h views recorded yet. Views come from "
            "view snapshots (YouTube poller, `cf import-reconcile`) once 24 h / 72 h have passed."
        )
    header = (
        f"{'archetype':<16} {'posts':>5} {'avg score':>9} {'n24':>4} {'med 24h':>9} {'avg 24h':>9} "
        f"{'n72':>4} {'med 72h':>9} {'avg 72h':>9}  weights"
    )
    lines = ["Per-archetype performance (live posts; views per post, each platform counted separately)", header]
    for s in stats:
        lines.append(
            f"{s.archetype:<16} {s.posts:>5} {statistics.fmean(s.scores):>9.0f} {len(s.views_24h):>4} "
            f"{_median(s.views_24h):>9} {_mean(s.views_24h):>9} {len(s.views_72h):>4} "
            f"{_median(s.views_72h):>9} {_mean(s.views_72h):>9}  {', '.join(sorted(s.weights)) or '-'}"
        )
    lines.append("")
    lines.append("Median 72 h views by platform:")
    for s in stats:
        parts = [f"{p} {_median(v)} (n={len(v)})" for p, v in sorted(s.by_platform_72h.items())]
        lines.append(f"  {s.archetype}: {', '.join(parts) or '-'}")
    return "\n".join(lines)


def moments_report(session: Session, settings: Settings) -> str:
    """`cf report moments`: fill due 24 h / 72 h marks, then print per-archetype performance."""
    del settings  # same signature as the other `cf` targets
    filled = fill_view_marks(session)
    text = render_report(archetype_stats(session))
    if filled:
        text = f"(filled {filled} 24 h / 72 h view value(s) from snapshots)\n{text}"
    print(text)
    return text
