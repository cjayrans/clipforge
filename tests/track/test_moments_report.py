"""Moment calibration: 24 h / 72 h view marks from snapshots, the per-archetype report, and the review preview."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, PostRow, ViewSnapshotRow
from clipforge.review.views import preview_caption
from clipforge.schemas import Platform
from clipforge.track.moments import archetype_stats, fill_view_marks, moments_report, render_report
from tests.support.factories import Factory
from tests.track.helpers import T0, real_post


def moment_post(factory: Factory, session: Session, archetype: str | None, **kw: Any) -> PostRow:
    post = real_post(factory, session, **kw)
    clip = session.get(ClipRow, post.clip_id)
    assert clip is not None
    cand = session.get(CandidateRow, clip.candidate_id)
    assert cand is not None
    if archetype is not None:
        cand.archetype = archetype
        cand.moment_profile = "gta6-streamer"
        cand.weights_version = "gta6-streamer-v1"
        cand.score_reaction, cand.score_event, cand.score_setup, cand.score_absurdity = 8, 6, 7, 9
    session.flush()
    return post


def snap(session: Session, post: PostRow, hours: float, views: int) -> None:
    assert post.published_at is not None
    session.add(
        ViewSnapshotRow(
            post_id=post.id, taken_at=post.published_at + timedelta(hours=hours), views=views, source="manual"
        )
    )
    session.flush()


def test_fill_view_marks_uses_the_nearest_snapshot_and_never_overwrites(factory: Factory, session: Session) -> None:
    post = moment_post(factory, session, "blunder")
    snap(session, post, 2, 100)
    snap(session, post, 23, 900)
    snap(session, post, 26, 1000)
    snap(session, post, 90, 5000)  # too far from 72 h
    now = T0 + timedelta(days=5)
    assert fill_view_marks(session, now) == 1
    assert post.views_24h == 900
    assert post.views_72h is None
    post.views_24h = 777  # entered by hand
    snap(session, post, 71, 4000)
    assert fill_view_marks(session, now) == 1
    assert post.views_24h == 777
    assert post.views_72h == 4000


def test_marks_wait_until_due_and_skip_dry_run_posts(factory: Factory, session: Session) -> None:
    post = moment_post(factory, session, "blunder")
    snap(session, post, 24, 900)
    assert fill_view_marks(session, T0 + timedelta(hours=23)) == 0
    fake = moment_post(factory, session, "blunder", url="https://example.invalid/dry/1")
    snap(session, fake, 24, 900)
    assert fill_view_marks(session, T0 + timedelta(days=1, hours=1)) == 1
    assert fake.views_24h is None


def test_report_groups_by_archetype(factory: Factory, session: Session, settings: Settings, capsys) -> None:
    assert render_report(archetype_stats(session)) == "No live posts from moment-ranked clips yet."
    a = moment_post(factory, session, "blunder")
    b = moment_post(factory, session, "blunder", platform=Platform.INSTAGRAM)
    c = moment_post(factory, session, "npc_weirdness")
    moment_post(factory, session, None)  # a transcript-window clip is not part of the report
    assert "no 24 h / 72 h views recorded yet" in render_report(archetype_stats(session))
    a.views_24h, a.views_72h = 1000, 3000
    b.views_24h, b.views_72h = 2000, 5000
    c.views_24h, c.views_72h = 100, 200
    session.flush()
    text = moments_report(session, settings)
    assert text == capsys.readouterr().out.strip()
    lines = text.splitlines()
    assert lines[2].split()[:3] == ["blunder", "2", "80"]  # best median 72 h first
    assert "4,000" in lines[2]
    assert "gta6-streamer-v1" in lines[2]
    assert lines[3].startswith("npc_weirdness")
    assert "blunder: instagram 5,000 (n=1), tiktok 3,000 (n=1)" in text


def test_preview_shows_the_moment_and_the_manual_check(factory: Factory, session: Session, settings: Settings) -> None:
    cand = factory.candidate(
        hook="He sold his car", archetype="blunder", review_flags=["sexual"], skip_flags=["cutscene_part"]
    )
    cand.score_reaction, cand.score_event, cand.score_setup, cand.score_absurdity = 9, 6, 7, 8
    clip = factory.clip(cand)
    text = preview_caption(session, settings, clip)
    assert "Moment: blunder | reaction 9, event 6, setup 7, absurdity 8" in text
    assert text.splitlines()[-2:] == [
        "Skip rules (score lowered): cutscene_part",
        "CHECK BEFORE APPROVING: flagged sexual",
    ]
    plain = factory.clip(factory.candidate(hook="talk"))
    assert "Moment:" not in preview_caption(session, settings, plain)
    assert "CHECK" not in preview_caption(session, settings, plain)


# --- moments-v2: viewer-response metrics and rubric correlations ---------------------------------------


def v2_post(factory: Factory, session: Session, scores: dict[str, int], **kw: Any) -> PostRow:
    post = moment_post(factory, session, kw.pop("archetype", "blunder"), **kw)
    clip = session.get(ClipRow, post.clip_id)
    cand = session.get(CandidateRow, clip.candidate_id)
    cand.weights_version = "gta6-streamer-v2"
    cand.scores_json = scores
    session.flush()
    return post


def test_post_metrics_are_stored_and_logged(factory: Factory, session: Session, settings: Settings) -> None:
    import pytest
    from sqlalchemy import select

    from clipforge.db import EventRow
    from clipforge.track.moments import MetricsError, set_post_metrics

    post = v2_post(factory, session, {"hook": 7})
    text = set_post_metrics(session, settings, post.id, watch_time_s=11.5, completion=42, shares=30, likes=400)
    assert post.completion_rate == 0.42  # a percentage becomes 0-1
    assert (post.avg_watch_time_s, post.shares, post.likes) == (11.5, 30, 400)
    assert post.metrics_at is not None
    assert "completion_rate=0.42" in text
    event = session.scalars(select(EventRow).where(EventRow.entity == "post")).all()[-1]
    assert event.actor == "operator"
    assert "metrics" in event.detail
    set_post_metrics(session, settings, post.id, completion=0.5)
    assert post.completion_rate == 0.5
    assert post.shares == 30  # untouched
    with pytest.raises(MetricsError, match="at least one"):
        set_post_metrics(session, settings, post.id)
    with pytest.raises(MetricsError, match="not found"):
        set_post_metrics(session, settings, 99_999, likes=1)


def test_spearman() -> None:
    from clipforge.track.moments import spearman

    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # no spread
    assert spearman([1, 2], [1, 2]) is None  # too few


def test_correlations_appear_from_20_posts(factory: Factory, session: Session, settings: Settings) -> None:
    from clipforge.track.moments import render_correlations, response_points, set_post_metrics

    posts = []
    for k in range(19):
        scores = {"hook": k % 10, "arc": 5, "arousal": (k * 3) % 10, "sendability": 9 - k % 10, "clarity": 5,
                  "density": 4}  # fmt: skip
        platform = Platform.TIKTOK if k % 2 else Platform.INSTAGRAM
        post = v2_post(factory, session, scores, platform=platform, archetype="blunder" if k % 3 else "glitch")
        set_post_metrics(session, settings, post.id, completion=(k % 10) / 10, shares=k, views_72h=1000)
        posts.append(post)
    text = render_correlations(response_points(session))
    assert "19 of 20 posts" in text
    post = v2_post(factory, session, {"hook": 9, "arc": 5, "arousal": 1, "sendability": 0, "clarity": 5, "density": 4})
    set_post_metrics(session, settings, post.id, completion=0.95, shares=50, views_72h=1000)
    text = render_correlations(response_points(session))
    assert "Spearman" in text
    assert "completion rate:" in text
    assert "shares per view:" in text
    assert "archetype blunder" in text
    assert "archetype glitch" in text
    assert "platform tiktok" in text
    assert "platform instagram" in text
    all_line = next(line for line in text.splitlines() if line.startswith("all posts"))
    assert "+1.00" in all_line  # hook tracks completion exactly here
    full = moments_report(session, settings)
    assert "Rubric score vs viewer response" in full
