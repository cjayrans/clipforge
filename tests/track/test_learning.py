"""D5: weekly learning - labels and few-shot selection."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ClipRow, FewshotRow, PostRow, TranscriptRow
from clipforge.schemas import BrandDecision, Platform, SubmissionState
from clipforge.track.learning import label_clips, run_weekly_learning, transcript_excerpt
from tests.support.factories import Factory
from tests.track.helpers import T0, real_post, submission, views

NOW = T0 + timedelta(days=10)  # the posts below are 10 days old: mature
WEEK = "2026-W46"  # 2026-11-11 is a Wednesday in ISO week 46


def candidate_of(session: Session, post: PostRow) -> int:
    clip = session.get(ClipRow, post.clip_id)
    assert clip is not None
    return clip.candidate_id


def kinds(session: Session) -> dict[int, str]:
    return {o.candidate_id: o.kind for o in label_clips(session, NOW)}


def test_paid_is_positive_and_rejected_is_negative(factory: Factory, session: Session) -> None:
    paid = real_post(factory, session)
    submission(factory, session, paid, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=4.0)
    rejected = real_post(factory, session)
    submission(factory, session, rejected, state=SubmissionState.SUBMITTED, brand=BrandDecision.REJECTED)
    assert kinds(session) == {candidate_of(session, paid): "positive", candidate_of(session, rejected): "negative"}


def test_rejected_counts_even_before_the_post_is_a_week_old(factory: Factory, session: Session) -> None:
    young = real_post(factory, session, published=NOW - timedelta(days=1))
    submission(factory, session, young, state=SubmissionState.SUBMITTED, brand=BrandDecision.REJECTED)
    assert kinds(session) == {candidate_of(session, young): "negative"}


def test_views_against_the_minimum_payout(factory: Factory, session: Session) -> None:
    camp = factory.campaign(cpm_usd=1.0, min_payout_usd=10.0)  # 10,000 views to reach the minimum
    hit = real_post(factory, session, campaign=camp)
    views(session, hit, 10_000, T0 + timedelta(days=3))
    middle = real_post(factory, session, campaign=camp)
    views(session, middle, 2_000, T0 + timedelta(days=3))
    low = real_post(factory, session, campaign=camp)
    views(session, low, 499, T0 + timedelta(days=3))
    assert kinds(session) == {candidate_of(session, hit): "positive", candidate_of(session, low): "negative"}


def test_no_view_data_is_not_a_negative(factory: Factory, session: Session) -> None:
    real_post(factory, session)
    assert kinds(session) == {}


def test_young_posts_are_not_labeled_by_views(factory: Factory, session: Session) -> None:
    young = real_post(factory, session, published=NOW - timedelta(days=2))
    views(session, young, 3, NOW - timedelta(days=1))
    assert kinds(session) == {}


def test_views_after_the_seven_day_window_do_not_count(factory: Factory, session: Session) -> None:
    camp = factory.campaign(cpm_usd=1.0, min_payout_usd=1.0)  # 1,000 views
    post = real_post(factory, session, campaign=camp)
    views(session, post, 50, T0 + timedelta(days=2))
    views(session, post, 900_000, T0 + timedelta(days=9))  # too late: outside "within 7 days"
    assert kinds(session) == {candidate_of(session, post): "negative"}


def test_zero_minimum_payout_is_not_a_threshold(factory: Factory, session: Session) -> None:
    post = real_post(factory, session, min_payout_usd=0.0)
    views(session, post, 300, T0 + timedelta(days=2))
    assert kinds(session) == {candidate_of(session, post): "negative"}


def test_dry_run_and_old_posts_are_ignored(factory: Factory, session: Session) -> None:
    fake = real_post(factory, session, url="https://example.invalid/tiktok/3")
    submission(factory, session, fake, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=9.0)
    ancient = real_post(factory, session, published=NOW - timedelta(days=60))
    submission(factory, session, ancient, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=9.0)
    assert kinds(session) == {}


def test_a_clip_with_one_good_platform_is_positive(factory: Factory, session: Session) -> None:
    tiktok = real_post(factory, session)
    submission(factory, session, tiktok, state=SubmissionState.SUBMITTED, brand=BrandDecision.REJECTED)
    clip = session.get(ClipRow, tiktok.clip_id)
    assert clip is not None
    from clipforge.db import transition
    from clipforge.schemas import PostState

    ig = factory.post(clip, Platform.INSTAGRAM)
    transition(session, ig, PostState.PUBLISHING)
    ig.post_url = "https://www.instagram.com/reel/abc/"
    ig.published_at = T0
    transition(session, ig, PostState.LIVE)
    submission(factory, session, ig, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=2.0)
    assert kinds(session) == {clip.candidate_id: "positive"}


# --- few-shots --------------------------------------------------------------------------------------------------------


def test_top_and_bottom_five_are_stored_and_the_job_is_idempotent(
    factory: Factory, session: Session, settings: Settings
) -> None:
    for i in range(7):
        p = real_post(factory, session, hook=f"win {i}")
        submission(factory, session, p, state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=1.0 + i)
    for i in range(7):
        p = real_post(factory, session, hook=f"flop {i}")
        views(session, p, 10 + i, T0 + timedelta(days=2))

    result = run_weekly_learning(session, settings, NOW)
    assert (result.week, result.positives, result.negatives) == (WEEK, 5, 5)
    rows = session.scalars(select(FewshotRow).order_by(FewshotRow.id)).all()
    assert len(rows) == 10
    positives = [r for r in rows if r.kind == "positive"]
    assert [r.hook for r in positives] == ["win 6", "win 5", "win 4", "win 3", "win 2"]  # best first
    assert positives[0].outcome_note == "paid $7.00"
    negatives = [r for r in rows if r.kind == "negative"]
    assert [r.hook for r in negatives][:2] == ["flop 0", "flop 1"]  # fewest views first
    assert "10 views in 7 days" in negatives[0].outcome_note
    assert {r.week for r in rows} == {WEEK}

    run_weekly_learning(session, settings, NOW)  # same week again: replaced, not duplicated
    assert len(session.scalars(select(FewshotRow)).all()) == 10
    run_weekly_learning(session, settings, NOW + timedelta(days=7))  # a new week adds its own rows
    assert {r.week for r in session.scalars(select(FewshotRow))} == {WEEK, "2026-W47"}


def test_rejected_negatives_come_before_low_view_negatives(
    factory: Factory, session: Session, settings: Settings
) -> None:
    low = real_post(factory, session, hook="low")
    views(session, low, 1, T0 + timedelta(days=2))
    rej = real_post(factory, session, hook="rejected")
    submission(factory, session, rej, state=SubmissionState.SUBMITTED, brand=BrandDecision.REJECTED)
    views(session, rej, 400, T0 + timedelta(days=2))
    run_weekly_learning(session, settings, NOW, top=1)
    (row,) = session.scalars(select(FewshotRow).where(FewshotRow.kind == "negative")).all()
    assert row.hook == "rejected"
    assert "rejected by the brand" in row.outcome_note


def test_nothing_to_learn_stores_nothing(session: Session, settings: Settings) -> None:
    result = run_weekly_learning(session, settings, NOW)
    assert (result.positives, result.negatives) == (0, 0)


# --- excerpts ----------------------------------------------------------


def test_excerpt_comes_from_the_transcript_words(
    factory: Factory, session: Session, settings: Settings, tmp_path: Path
) -> None:
    post = real_post(factory, session)
    clip = session.get(ClipRow, post.clip_id)
    assert clip is not None
    from clipforge.db import CandidateRow

    candidate = session.get(CandidateRow, clip.candidate_id)
    assert candidate is not None
    words = [
        {"text": "before", "start_s": 2.0, "end_s": 2.4},
        {"text": "hello", "start_s": 12.0, "end_s": 12.4},
        {"text": "there", "start_s": 12.5, "end_s": 12.9},
        {"text": "after", "start_s": 55.0, "end_s": 55.4},
    ]
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"words": words}), encoding="utf-8")
    session.add(TranscriptRow(source_id=candidate.source_id, provider="groq", model="m", words_json_path=str(path)))
    session.flush()
    assert transcript_excerpt(session, settings, candidate) == "hello there"


def test_excerpt_falls_back_to_the_ranking_reasons(factory: Factory, session: Session, settings: Settings) -> None:
    post = real_post(factory, session)
    clip = session.get(ClipRow, post.clip_id)
    assert clip is not None
    from clipforge.db import CandidateRow

    candidate = session.get(CandidateRow, clip.candidate_id)
    assert candidate is not None
    candidate.reasons = "funny reaction to the trailer"
    assert transcript_excerpt(session, settings, candidate) == "funny reaction to the trailer"
    session.add(
        TranscriptRow(source_id=candidate.source_id, provider="groq", model="m", words_json_path="missing.json")
    )
    session.flush()
    assert transcript_excerpt(session, settings, candidate) == "funny reaction to the trailer"
