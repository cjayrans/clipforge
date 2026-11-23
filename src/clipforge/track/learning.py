"""Weekly learning (PRD F12, docs/ARCHITECTURE.md §4.8): label outcomes, store top/bottom 5 as few-shots for ranking.

Labels per post (a clip's label is positive if any of its posts is positive, else negative if any is negative):
- positive: the submission was paid, or the post reached the campaign's minimum-payout views within 7 days;
- negative: the brand rejected it, or it stayed under 500 views within 7 days;
- unlabeled: no outcome yet, or no view data. Missing data is never treated as "0 views".

Only real posts count (dry_run fake URLs are ignored). Views come from `view_snapshots` (YouTube API or manual from the
reconcile CSV). Re-running in the same ISO week replaces that week's rows, so the job is idempotent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, FewshotRow, PostRow, SubmissionRow, TranscriptRow
from clipforge.publish.queries import lineage
from clipforge.schemas import BrandDecision
from clipforge.track.metrics import iso_week, live_real_posts, min_payout_views, views_within

log = logging.getLogger(__name__)

NEGATIVE_VIEWS = 500
WINDOW_DAYS = 7
LOOKBACK_DAYS = 35
TOP_N = 5
EXCERPT_CHARS = 600


@dataclass
class ClipOutcome:
    candidate_id: int
    kind: str = "unlabeled"  # positive | negative | unlabeled
    views: int | None = None
    paid_usd: float = 0.0
    rejected: bool = False
    post_ids: list[int] = field(default_factory=list)


@dataclass
class LearningResult:
    week: str
    positives: int
    negatives: int


def _label_post(session: Session, post: PostRow, now: datetime) -> tuple[str, int | None, float, bool]:
    sub = session.scalar(select(SubmissionRow).where(SubmissionRow.post_id == post.id))
    paid = sub.paid_usd if sub is not None else 0.0
    rejected = sub is not None and sub.brand_decision is BrandDecision.REJECTED
    views = views_within(session, post, WINDOW_DAYS)
    if paid > 0:
        return "positive", views, paid, rejected
    if rejected:
        return "negative", views, paid, rejected
    mature = post.published_at is not None and post.published_at <= now - timedelta(days=WINDOW_DAYS)
    if mature and views is not None:
        clip = session.get(ClipRow, post.clip_id)
        if clip is not None:
            minimum = min_payout_views(lineage(session, clip).campaign, post)
            if minimum is not None and minimum > 0 and views >= minimum:
                return "positive", views, paid, rejected
        if views < NEGATIVE_VIEWS:
            return "negative", views, paid, rejected
    return "unlabeled", views, paid, rejected


def label_clips(session: Session, now: datetime) -> list[ClipOutcome]:
    outcomes: dict[int, ClipOutcome] = {}
    for post in live_real_posts(session):
        if post.published_at is None or post.published_at < now - timedelta(days=LOOKBACK_DAYS):
            continue
        clip = session.get(ClipRow, post.clip_id)
        if clip is None:
            continue
        kind, views, paid, rejected = _label_post(session, post, now)
        out = outcomes.setdefault(clip.candidate_id, ClipOutcome(clip.candidate_id))
        out.post_ids.append(post.id)
        out.paid_usd += paid
        out.rejected = out.rejected or rejected
        if views is not None:
            out.views = max(out.views or 0, views)
        if kind == "positive" or (kind == "negative" and out.kind != "positive"):
            out.kind = kind
    return [o for o in outcomes.values() if o.kind != "unlabeled"]


def transcript_excerpt(session: Session, settings: Settings, candidate: CandidateRow) -> str:
    """The words spoken in the candidate's range, from the stored transcript JSON; the ranking reasons as fallback."""
    row = session.scalars(select(TranscriptRow).where(TranscriptRow.source_id == candidate.source_id)).first()
    if row is not None:
        for path in (Path(row.words_json_path), settings.data_dir / row.words_json_path):
            try:
                data: Any = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            words = data.get("words") if isinstance(data, dict) else None
            if isinstance(words, list):
                text = " ".join(
                    str(w.get("text", "")).strip()
                    for w in words
                    if isinstance(w, dict)
                    and candidate.start_s
                    <= float(w.get("start_s", -1))
                    <= float(w.get("end_s", 0))
                    <= candidate.end_s + 0.5
                )
                if text.strip():
                    return text[:EXCERPT_CHARS]
    return (candidate.reasons or "")[:EXCERPT_CHARS]


def _note(out: ClipOutcome) -> str:
    parts: list[str] = []
    if out.rejected:
        parts.append("rejected by the brand")
    if out.paid_usd > 0:
        parts.append(f"paid ${out.paid_usd:.2f}")
    if out.views is not None:
        parts.append(f"{out.views:,} views in {WINDOW_DAYS} days")
    return "; ".join(parts) or "no data"


def run_weekly_learning(session: Session, settings: Settings, now: datetime, *, top: int = TOP_N) -> LearningResult:
    """Replace this ISO week's few-shots with the top and bottom `top` clips. Returns how many of each were stored."""
    week = iso_week(now, settings.app.tz)
    outcomes = label_clips(session, now)
    positives = sorted(
        (o for o in outcomes if o.kind == "positive"), key=lambda o: (o.paid_usd, o.views or 0), reverse=True
    )[:top]
    negatives = sorted((o for o in outcomes if o.kind == "negative"), key=lambda o: (not o.rejected, o.views or 0))[
        :top
    ]
    session.execute(delete(FewshotRow).where(FewshotRow.week == week))
    for kind, group in (("positive", positives), ("negative", negatives)):
        for out in group:
            candidate = session.get(CandidateRow, out.candidate_id)
            if candidate is None:
                continue
            session.add(
                FewshotRow(
                    kind=kind,
                    candidate_id=candidate.id,
                    hook=candidate.hook,
                    transcript_excerpt=transcript_excerpt(session, settings, candidate),
                    outcome_note=_note(out),
                    week=week,
                )
            )
    session.flush()
    return LearningResult(week, len(positives), len(negatives))
