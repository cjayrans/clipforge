"""Invariant 9: X captions contain no URLs; political campaigns never publish to TikTok or X."""

from __future__ import annotations

import pytest

from clipforge.compliance import (
    ComplianceError,
    allowed_platforms,
    assert_platform_allowed,
    contains_url,
    normalize_caption,
)
from clipforge.config import AppConfig
from clipforge.schemas import Platform, PublishMode, PublishRequest
from tests.compliance._helpers import campaign


@pytest.mark.parametrize(
    "raw",
    [
        "watch the full stream https://twitch.tv/someone",
        "full vod at www.example.com/vod?t=1",
        "follow twitch.tv/someone for more",
        "links: http://a.co/x and HTTPS://B.IO",
        "clip from youtube.com/watch?v=1 lol",
    ],
)
def test_x_caption_has_no_urls(raw: str) -> None:
    c = campaign(disclosure_text="#ad see example.com", required_tags=["#GTA6"])
    out = normalize_caption(raw, Platform.X, c)
    assert not contains_url(out)
    assert out.startswith("#ad ")
    assert "#GTA6" in out


def test_urls_kept_on_other_platforms() -> None:
    out = normalize_caption("vod at twitch.tv/someone", Platform.TIKTOK, campaign())
    assert "twitch.tv/someone" in out


def test_hashtags_and_versions_are_not_urls() -> None:
    assert not contains_url("#GTA6 v1.0 is out, 10.5 stars, #DylanPage")


def test_political_blocked_tiktok_x() -> None:
    all_four = ["tiktok", "instagram", "youtube", "x"]
    political = campaign(allowed_platforms=all_four, is_political=True)
    assert allowed_platforms(political) == [Platform.INSTAGRAM, Platform.YOUTUBE]
    for p in (Platform.TIKTOK, Platform.X):
        with pytest.raises(ComplianceError):
            assert_platform_allowed(political, p)
    normal = campaign(allowed_platforms=all_four)
    assert allowed_platforms(normal, [Platform.TIKTOK, Platform.INSTAGRAM]) == [Platform.TIKTOK, Platform.INSTAGRAM]


def test_platform_not_in_campaign_refused() -> None:
    c = campaign(allowed_platforms=["tiktok"])
    with pytest.raises(ComplianceError):
        assert_platform_allowed(c, Platform.INSTAGRAM)
    with pytest.raises(ComplianceError):
        assert_platform_allowed(c, Platform.TIKTOK, enabled=[Platform.INSTAGRAM])


def test_upload_payload_x_caption_has_no_urls(app_config: AppConfig) -> None:
    from clipforge.publish.upload_post import build_payload  # type: ignore[attr-defined]

    req = PublishRequest(
        post_id=1,
        clip_id=1,
        platform=Platform.X,
        upload_post_user="gaming-1",
        video_path="/tmp/c.mp4",
        caption="#ad watch https://twitch.tv/x",
        mode=PublishMode.DRY_RUN,
    )
    payload = build_payload(req, campaign(), app_config)
    assert not any(contains_url(str(v)) for k, v in payload.items() if "title" in k or "description" in k)
