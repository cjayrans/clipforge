"""Invariant 2: every caption on every platform starts with '#ad ' (exact, then a space)."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.compliance import AD_PREFIX, CAPTION_LIMITS, ComplianceError, caption_length, normalize_caption
from clipforge.config import AppConfig
from clipforge.schemas import Platform, PublishMode, PublishRequest
from tests.compliance._helpers import campaign

RAW_CAPTIONS = [
    "This heist went sideways fast",
    "#ad This heist went sideways fast",
    "#AD, this heist",
    "#ad #ad #ad double disclosure",
    "#advertisement is not a disclosure",
    "   leading spaces",
    "",
]


@pytest.mark.parametrize("platform", list(Platform))
@pytest.mark.parametrize("raw", RAW_CAPTIONS)
def test_caption_starts_with_ad_all_platforms(platform: Platform, raw: str) -> None:
    c = campaign(required_tags=["#DylanPage"])
    out = normalize_caption(raw, platform, c)
    assert out.startswith(AD_PREFIX + " ")
    assert re.match(r"(?i)#ad\s+#ad\b", out) is None  # disclosure not doubled
    assert "#DylanPage" in out
    assert caption_length(out, platform) <= CAPTION_LIMITS[platform]


def test_hashtag_starting_with_ad_is_not_a_disclosure() -> None:
    out = normalize_caption("#advertisement", Platform.TIKTOK, campaign())
    assert out == "#ad #advertisement"


def test_custom_disclosure_text_kept_after_ad() -> None:
    out = normalize_caption("clip", Platform.INSTAGRAM, campaign(disclosure_text="#ad #sponsored"))
    assert out == "#ad #sponsored clip"
    out = normalize_caption("clip", Platform.INSTAGRAM, campaign(disclosure_text="Paid partnership"))
    assert out == "#ad Paid partnership clip"


def test_required_tags_not_duplicated() -> None:
    out = normalize_caption("love this #dylanpage", Platform.TIKTOK, campaign(required_tags=["#DylanPage"]))
    assert out.lower().count("#dylanpage") == 1


def test_empty_caption_refused() -> None:
    with pytest.raises(ComplianceError):
        normalize_caption("  #ad ", Platform.TIKTOK, campaign())


def test_long_caption_trimmed_keeping_prefix_and_tags() -> None:
    long = "word " * 1000
    for platform in Platform:
        out = normalize_caption(long, platform, campaign(required_tags=["#GTA6"]))
        assert out.startswith("#ad ")
        assert out.endswith("#GTA6")
        assert caption_length(out, platform) <= CAPTION_LIMITS[platform]


@given(st.text(max_size=3000), st.sampled_from(list(Platform)))
def test_caption_property(raw: str, platform: Platform) -> None:
    try:
        out = normalize_caption(raw, platform, campaign(required_tags=["#GTA6"]))
    except ComplianceError:
        return
    assert out.startswith("#ad ")
    assert caption_length(out, platform) <= CAPTION_LIMITS[platform]


@pytest.mark.parametrize("platform", list(Platform))
def test_upload_payload_caption_starts_with_ad(platform: Platform, app_config: AppConfig) -> None:
    from clipforge.publish.upload_post import build_payload  # type: ignore[attr-defined]

    req = PublishRequest(
        post_id=1,
        clip_id=1,
        platform=platform,
        upload_post_user="gaming-1",
        video_path="/tmp/c.mp4",
        caption="no disclosure here",
        youtube_title="A title",
        mode=PublishMode.DRY_RUN,
    )
    payload = build_payload(req, campaign(), app_config)
    keys = (
        ["youtube_description", "description"] if platform is Platform.YOUTUBE else [f"{platform.value}_title", "title"]
    )
    caption = next(str(payload[k]) for k in keys if k in payload)
    assert caption.startswith("#ad ")
