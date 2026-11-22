"""Contract tests for src/clipforge/schemas.py."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from clipforge import schemas as s


def _campaign(**overrides: object) -> s.CampaignIn:
    data: dict[str, object] = {
        "marketplace": "content_rewards",
        "name": "Test Campaign",
        "url": "https://contentrewards.com/c/test",
        "submission_url": "https://contentrewards.com/c/test/submit",
        "cpm_usd": 1.0,
        "allowed_platforms": ["tiktok", "instagram"],
    }
    data.update(overrides)
    return s.CampaignIn.model_validate(data)


def test_transition_tables_cover_every_state() -> None:
    assert set(s.CLIP_TRANSITIONS) == set(s.ClipState)
    assert set(s.POST_TRANSITIONS) == set(s.PostState)
    assert set(s.SUBMISSION_TRANSITIONS) == set(s.SubmissionState)
    for table in (s.CLIP_TRANSITIONS, s.POST_TRANSITIONS, s.SUBMISSION_TRANSITIONS):
        for state, targets in table.items():
            assert state not in targets


def test_terminal_clip_states() -> None:
    for state in (s.ClipState.QA_FAILED, s.ClipState.REJECTED, s.ClipState.PUBLISHED, s.ClipState.EXPIRED):
        assert s.CLIP_TRANSITIONS[state] == frozenset()


def test_campaign_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        _campaign(unexpected=True)


def test_campaign_needs_a_platform() -> None:
    with pytest.raises(ValidationError):
        _campaign(allowed_platforms=[])


def test_cpm_and_min_payout_fallbacks() -> None:
    c = _campaign(cpm_by_platform={"tiktok": 2.0}, min_payout_usd=4.0, min_payout_by_platform={"x": 1.0})
    assert c.cpm_for(s.Platform.TIKTOK) == 2.0
    assert c.cpm_for(s.Platform.INSTAGRAM) == 1.0
    assert c.min_payout_for(s.Platform.X) == 1.0
    assert c.min_payout_for(s.Platform.TIKTOK) == 4.0


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Dylan Page GTA VI Marathon (NewsDaddy)", "dylan-page-gta-vi-marathon-newsdaddy"),
        ("  !!!  ", "campaign"),
        ("Ünïcode & Co", "n-code-co"),
    ],
)
def test_slug(name: str, slug: str) -> None:
    assert _campaign(name=name).slug == slug


@pytest.mark.parametrize(("start", "end", "ok"), [(0, 20, True), (0, 75, True), (0, 19.9, False), (10, 85.1, False)])
def test_window_length_bounds(start: float, end: float, ok: bool) -> None:
    kwargs = {"window_id": "w1", "source_id": 1, "start_s": start, "end_s": end, "text": "hi"}
    if ok:
        s.Window(**kwargs)
    else:
        with pytest.raises(ValidationError):
            s.Window(**kwargs)


def test_clip_copy_hook_word_limit() -> None:
    s.ClipCopy(hook="one two three four five six seven eight", youtube_title="t", captions=[])
    with pytest.raises(ValidationError):
        s.ClipCopy(hook="one two three four five six seven eight nine", youtube_title="t", captions=[])


def test_candidate_copy_alias() -> None:
    copy = s.ClipCopy(hook="hook", youtube_title="title", captions=[])
    cand = s.Candidate.model_validate(
        {
            "source_id": 1,
            "start_s": 0,
            "end_s": 30,
            "score": 80,
            "standalone": True,
            "brief_ok": True,
            "reasons": "r",
            "copy": copy.model_dump(),
            "prompt_version": "v1",
        }
    )
    assert cand.copy_ == copy
    assert cand.state is s.ClipState.CANDIDATE


def test_qa_report_passed() -> None:
    ok = s.QAReport(clip_path="a.mp4", checks=[s.QACheck(name="a", passed=True)])
    bad = s.QAReport(clip_path="a.mp4", checks=[s.QACheck(name="a", passed=True), s.QACheck(name="b", passed=False)])
    assert ok.passed
    assert not bad.passed


def test_evening_armed() -> None:
    assert not s.Evening(date=date(2026, 11, 23)).armed
    armed_at = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
    assert s.Evening(date=date(2026, 11, 23), armed_at=armed_at).armed
    assert not s.Evening(date=date(2026, 11, 23), armed_at=armed_at, skipped=True).armed


def test_source_defaults() -> None:
    src = s.Source(id=1, campaign_id=2, url="https://example.com/a.mp4")
    assert src.status is s.SourceStatus.REGISTERED
    assert src.fail_reason is None
