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
