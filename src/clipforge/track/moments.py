"""Moment-ranker calibration: 24 h / 72 h views per post, and per-archetype performance (`cf report moments`).

`posts.views_24h` / `views_72h` are filled from `view_snapshots` (YouTube poller, reconcile imports) once the mark
has passed: the snapshot nearest the mark, if one was taken within MARK_TOLERANCE of it. A value already stored is
never overwritten, so a hand-entered number wins. Only live posts with a real URL count (dry-run posts never do).

Writing a view number is not a state change, so no `events` row is written.

moments-v2 calibration: the operator enters viewer-response numbers per post with `cf post metrics` (average watch
time, completion rate, shares/sends, likes, 24 h / 72 h views; an `events` row records each entry). Once at least
`MIN_POSTS_FOR_CORRELATION` posts have completion or share data, `cf report moments` also shows how each rubric score
correlates (Spearman) with completion rate and with shares per view, overall, per archetype and per platform.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, PostRow, ViewSnapshotRow, record_event, utcnow
from clipforge.schemas import PostState
from clipforge.track.metrics import is_fake_url

MARKS: dict[str, timedelta] = {"views_24h": timedelta(hours=24), "views_72h": timedelta(hours=72)}
MARK_TOLERANCE = timedelta(hours=6)
MIN_POSTS_FOR_CORRELATION = 20
LOW_N = 5  # cells with fewer posts are marked
MIN_N = 3  # cells with fewer posts show no number
RUBRIC_ORDER = ("hook", "arc", "arousal", "sendability", "clarity", "density")


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


# --------------------------------------------------------------------------------------------------
# Viewer-response metrics and rubric correlations (moments-v2)
# --------------------------------------------------------------------------------------------------


class MetricsError(ValueError):
    pass


def set_post_metrics(
    session: Session,
    settings: Settings,
    post_id: int,
    *,
    watch_time_s: float | None = None,
    completion: float | None = None,
    shares: int | None = None,
    likes: int | None = None,
    views_24h: int | None = None,
    views_72h: int | None = None,
    now: datetime | None = None,
) -> str:
    """`cf post metrics`: store the numbers given (others unchanged). Completion is 0-1 or a percentage (0-100)."""
    del settings
    post = session.get(PostRow, post_id)
    if post is None:
        raise MetricsError(f"post {post_id} not found")
    values: dict[str, float | int] = {}
    if completion is not None:
        if completion < 0 or completion > 100:
            raise MetricsError("completion must be 0-1 or a percentage 0-100")
        values["completion_rate"] = round(completion / 100 if completion > 1 else completion, 4)
    for name, value in (
        ("avg_watch_time_s", watch_time_s),
        ("shares", shares),
        ("likes", likes),
        ("views_24h", views_24h),
        ("views_72h", views_72h),
    ):
        if value is not None:
            if value < 0:
                raise MetricsError(f"{name} must be >= 0")
            values[name] = value
    if not values:
        raise MetricsError("give at least one metric")
    for name, value in values.items():
        setattr(post, name, value)
    post.metrics_at = now or utcnow()
    detail = ", ".join(f"{k}={v}" for k, v in values.items())
    record_event(session, "post", post.id, None, None, actor="operator", detail=f"metrics: {detail}")
    session.flush()
    return f"post {post_id} ({post.platform.value}): {detail}"


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation (ties averaged), or None with fewer than MIN_N pairs or no variation."""
    if len(xs) != len(ys) or len(xs) < MIN_N:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy) ** 0.5


@dataclass(frozen=True)
class PostPoint:
    archetype: str
    platform: str
    scores: dict[str, int]
    completion: float | None
    shares_per_view: float | None


def _scores_of(cand: CandidateRow) -> dict[str, int]:
    if isinstance(cand.scores_json, dict):
        return {str(k): int(v) for k, v in cand.scores_json.items() if isinstance(v, int | float)}
    return {}


def response_points(session: Session) -> list[PostPoint]:
    """Live posts of rubric-scored clips with completion or share data."""
    rows = session.execute(
        select(PostRow, CandidateRow)
        .join(ClipRow, ClipRow.id == PostRow.clip_id)
        .join(CandidateRow, CandidateRow.id == ClipRow.candidate_id)
        .where(PostRow.state == PostState.LIVE, CandidateRow.archetype.is_not(None))
    ).all()
    out: list[PostPoint] = []
    for post, cand in rows:
        if is_fake_url(post.post_url):
            continue
        scores = _scores_of(cand)
        views = post.views_72h or post.views_24h
        spv = post.shares / views if post.shares is not None and views else None
        if not scores or (post.completion_rate is None and spv is None):
            continue
        assert cand.archetype is not None
        out.append(PostPoint(cand.archetype, post.platform.value, scores, post.completion_rate, spv))
    return out


def _cell(points: list[PostPoint], key: str, metric: str) -> str:
    pairs = [
        (float(p.scores[key]), float(v)) for p in points if key in p.scores and (v := getattr(p, metric)) is not None
    ]
    rho = spearman([a for a, _ in pairs], [b for _, b in pairs])
    if rho is None:
        return f"{'-':>6} n={len(pairs):<3}"
    mark = "*" if len(pairs) < LOW_N else " "
    return f"{rho:+.2f}{mark} n={len(pairs):<3}"


def render_correlations(points: list[PostPoint]) -> str:
    if len(points) < MIN_POSTS_FOR_CORRELATION:
        return (
            f"Rubric correlations: {len(points)} of {MIN_POSTS_FOR_CORRELATION} posts have completion or share data "
            "(`cf post metrics`); shown from 20."
        )
    keys = [k for k in RUBRIC_ORDER if any(k in p.scores for p in points)]
    keys += sorted({k for p in points for k in p.scores} - set(keys))
    groups: list[tuple[str, list[PostPoint]]] = [("all posts", points)]
    groups += [
        (f"archetype {a}", [p for p in points if p.archetype == a]) for a in sorted({p.archetype for p in points})
    ]
    groups += [
        (f"platform {pl}", [p for p in points if p.platform == pl]) for pl in sorted({p.platform for p in points})
    ]
    lines = [
        "Rubric score vs viewer response (Spearman rho; * = fewer than 5 posts; - = too few or no spread)",
    ]
    for metric, label in (("completion", "completion rate"), ("shares_per_view", "shares per view")):
        lines.append("")
        lines.append(f"{label}:")
        lines.append(f"{'':<24}" + "".join(f"{k:>15}" for k in keys))
        for name, pts in groups:
            lines.append(f"{name:<24}" + "".join(f"{_cell(pts, k, metric):>15}" for k in keys))
    return "\n".join(lines)


def moments_report(session: Session, settings: Settings) -> str:
    """`cf report moments`: fill due 24 h / 72 h marks, then print per-archetype performance and, from 20 posts with
    viewer data, rubric correlations."""
    del settings  # same signature as the other `cf` targets
    filled = fill_view_marks(session)
    text = render_report(archetype_stats(session)) + "\n\n" + render_correlations(response_points(session))
    if filled:
        text = f"(filled {filled} 24 h / 72 h view value(s) from snapshots)\n{text}"
    print(text)
    return text
