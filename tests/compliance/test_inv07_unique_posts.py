"""Invariant 7: one submission per post; no clip (or clip file) posted twice to the same platform."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from clipforge.compliance import ComplianceError, ExistingPost, assert_unique_post
from clipforge.schemas import Platform
from tests.support.factories import Factory


def test_no_duplicate_platform_post(factory: Factory, session: Session) -> None:
    clip = factory.clip()
    factory.post(clip, Platform.TIKTOK)
    factory.post(clip, Platform.INSTAGRAM)  # other platform: fine
    with pytest.raises(IntegrityError):
        factory.post(clip, Platform.TIKTOK)


def test_same_file_twice_refused_by_db(factory: Factory, session: Session) -> None:
    a = factory.clip()
    b = factory.clip(sha256=a.sha256)  # same file under another clip row
    factory.post(a, Platform.TIKTOK)
    with pytest.raises(IntegrityError):
        factory.post(b, Platform.TIKTOK)


def test_one_submission_per_post(factory: Factory, session: Session) -> None:
    post = factory.post()
    factory.submission(post)
    with pytest.raises(IntegrityError):
        factory.submission(post)


def test_assert_unique_post() -> None:
    existing = [ExistingPost(1, "aaa", Platform.TIKTOK), ExistingPost(2, "bbb", Platform.INSTAGRAM)]
    assert_unique_post(1, "aaa", Platform.INSTAGRAM, existing)
    assert_unique_post(3, "ccc", Platform.TIKTOK, existing)
    with pytest.raises(ComplianceError, match="already has"):
        assert_unique_post(1, "zzz", Platform.TIKTOK, existing)
    with pytest.raises(ComplianceError, match="already posted"):
        assert_unique_post(9, "bbb", Platform.INSTAGRAM, existing)
