"""Invariant 4: paid-content flags always set, AI/synthetic flags false; no synthetic faces/voices.

One exception (docs/COMPLIANCE.md #4): a private-mode TikTok post is SELF_ONLY and carries no branded field, because
TikTok forbids branded content on a private post and Upload-Post may publish that combination publicly.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from clipforge.compliance import ComplianceError, assert_tiktok_disclosure
from clipforge.config import AppConfig
from clipforge.schemas import Platform, PublishMode, PublishRequest
from tests.compliance._helpers import campaign

SRC = Path(__file__).resolve().parents[2] / "src"

# Libraries that synthesize voices or faces. None may be imported anywhere in src/.
SYNTHETIC_MEDIA_LIBS = [
    "elevenlabs",
    "TTS",
    "bark",
    "pyttsx3",
    "gtts",
    "edge_tts",
    "openai.audio.speech",
    "diffusers",
    "deepface",
    "faceswap",
    "roop",
    "insightface.app.swapper",
    "sadtalker",
    "wav2lip",
]


def test_no_synthetic_media_libraries_in_src() -> None:
    pattern = re.compile(
        r"^\s*(?:import|from)\s+(" + "|".join(re.escape(m) for m in SYNTHETIC_MEDIA_LIBS) + r")\b", re.M
    )
    offenders = [str(p) for p in SRC.rglob("*.py") if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def _truthy(v: object) -> bool:
    return str(v).lower() == "true"


def _req(platform: Platform, mode: PublishMode, caption: str = "#ad clip") -> PublishRequest:
    return PublishRequest(
        post_id=1,
        clip_id=1,
        platform=platform,
        upload_post_user="gaming-1",
        video_path="/tmp/c.mp4",
        caption=caption,
        youtube_title="A title",
        mode=mode,
    )


def _branded_keys(payload: dict[str, Any]) -> list[str]:
    fragments = ("paidpartnership", "disclosecommercial", "branded", "yourbrand")
    compact = {k: re.sub(r"[\[\]_\-\s]", "", k.lower()) for k in payload}
    return [k for k, c in compact.items() if any(f in c for f in fragments)]


@pytest.mark.parametrize("mode", [PublishMode.DRY_RUN, PublishMode.LIVE])
@pytest.mark.parametrize("platform", list(Platform))
def test_disclosure_flags_per_platform(platform: Platform, mode: PublishMode, app_config: AppConfig) -> None:
    from clipforge.publish.upload_post import build_payload

    p = build_payload(_req(platform, mode), campaign(), app_config)
    if platform is Platform.TIKTOK:
        assert p["brand_content_toggle"] is True  # live/public always branded
        assert p["privacy_level"] == "PUBLIC_TO_EVERYONE"
        assert not _truthy(p["brand_organic_toggle"])
        assert not _truthy(p["is_aigc"])
    elif platform is Platform.YOUTUBE:
        assert _truthy(p["hasPaidProductPlacement"])
        assert not _truthy(p["containsSyntheticMedia"])
        assert not _truthy(p["selfDeclaredMadeForKids"])
    elif platform is Platform.INSTAGRAM:
        assert not _truthy(p["is_ai_generated"])
        assert "collaborators" not in p
    else:
        assert not _truthy(p["made_with_ai"])


# --- private-mode exception (TikTok) -----------------------------------------------------------------------------


@pytest.mark.parametrize("caption", ["#ad clip", "clip with no tag", "#AD, shouting", "  #ad   spaced"])
def test_tiktok_private_is_self_only_and_never_branded(caption: str, app_config: AppConfig) -> None:
    from clipforge.publish.upload_post import build_payload

    p = build_payload(_req(Platform.TIKTOK, PublishMode.PRIVATE, caption), campaign(), app_config)
    assert p["privacy_level"] == "SELF_ONLY"
    assert p["brand_content_toggle"] is False
    assert p["brand_organic_toggle"] is False
    assert p["is_aigc"] is False
    assert _branded_keys(p) == []
    # Invariant 2 is not part of the exception: '#ad ' still leads the caption.
    assert p["tiktok_title"].startswith("#ad ")
    assert p["title"].startswith("#ad ")


@pytest.mark.parametrize("caption", ["#ad clip", "clip with no tag"])
@pytest.mark.parametrize("mode", [PublishMode.LIVE, PublishMode.PRIVATE, PublishMode.DRY_RUN])
def test_tiktok_caption_starts_with_ad_in_every_mode(caption: str, mode: PublishMode, app_config: AppConfig) -> None:
    from clipforge.publish.upload_post import build_payload

    p = build_payload(_req(Platform.TIKTOK, mode, caption), campaign(), app_config)
    assert p["tiktok_title"].startswith("#ad ")


def test_guard_accepts_the_good_live_payload() -> None:
    payload = {**_good_private(), "privacy_level": "PUBLIC_TO_EVERYONE", "brand_content_toggle": True}
    assert_tiktok_disclosure(payload, private=False)
    assert_tiktok_disclosure({**payload, "brand_content_toggle": "true"}, private=False)


def test_youtube_private_keeps_paid_product_placement(app_config: AppConfig) -> None:
    """The private-mode exception is TikTok only: YouTube private still declares paid product placement."""
    from clipforge.publish.upload_post import build_payload

    p = build_payload(_req(Platform.YOUTUBE, PublishMode.PRIVATE), campaign(), app_config)
    assert p["privacyStatus"] == "private"
    assert p["hasPaidProductPlacement"] is True
    assert p["containsSyntheticMedia"] is False
    assert p["youtube_description"].startswith("#ad ")


def _good_private() -> dict[str, Any]:
    return {
        "user": "gaming-1",
        "platform[]": ["tiktok"],
        "tiktok_title": "#ad clip",
        "privacy_level": "SELF_ONLY",
        "post_mode": "DIRECT_POST",
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "is_aigc": False,
    }


def test_guard_accepts_the_good_private_payload_as_dict_and_form() -> None:
    assert_tiktok_disclosure(_good_private(), private=True)
    form = {k: ("false" if v is False else v) for k, v in _good_private().items()}
    assert_tiktok_disclosure(form, private=True)


@pytest.mark.parametrize(
    "mutation",
    [
        {"privacy_level": "PUBLIC_TO_EVERYONE"},
        {"privacy_level": "MUTUAL_FOLLOW_FRIENDS"},
        {"privacy_level": "FOLLOWER_OF_CREATOR"},
        {"privacy_level": "self_only"},
        {"privacy_level": None},
        {"tiktok_privacy_level": "PUBLIC_TO_EVERYONE"},
        {"brand_content_toggle": True},
        {"brand_content_toggle": "true"},
        {"brand_content_toggle": "1"},
        {"brand_content_toggle": None},
        {"brand_organic_toggle": "yes"},
        {"paid_partnership": True},
        {"paid_partnership": False},
        {"PAID-PARTNERSHIP": "false"},
        {"tiktok_paid_partnership": True},
        {"x_paid_partnership": True},
        {"disclose_commercial": True},
        {"disclose_commercial": False},
        {"branded_content": True},
        {"is_branded_content": True},
        {"your_brand": True},
        {"extra": {"nested": {"paid_partnership": True}}},
        {"paidPartnership": True},
        {"brandedContent": True},
        {"discloseCommercial": True},
        {"yourBrand": False},
        {"tiktok_brand_content_toggle": "true"},
        {"brandContentToggle": "true"},
        {"tiktok_brand_organic_toggle": True},
        {"privacyLevel": "PUBLIC_TO_EVERYONE"},
        {"privacy_level": ["SELF_ONLY"]},
    ],
)
def test_guard_raises_on_bad_private_payload(mutation: dict[str, Any]) -> None:
    payload = {**_good_private(), **mutation}
    with pytest.raises(ComplianceError) as exc:
        assert_tiktok_disclosure(payload, private=True)
    assert exc.value.invariant == 4


@pytest.mark.parametrize("toggle", ["brand_content_toggle", "brand_organic_toggle"])
def test_guard_raises_when_private_toggle_is_missing(toggle: str) -> None:
    payload = _good_private()
    del payload[toggle]
    with pytest.raises(ComplianceError):
        assert_tiktok_disclosure(payload, private=True)


@pytest.mark.parametrize(
    "mutation",
    [
        {"brand_content_toggle": False},
        {"brand_content_toggle": "false"},
        {"brand_content_toggle": None},
        {"tiktok_brand_content_toggle": "false"},
        {"brandContentToggle": False},
        {"privacy_level": "SELF_ONLY"},
        {"privacy_level": "MUTUAL_FOLLOW_FRIENDS"},
        {"privacy_level": "FOLLOWER_OF_CREATOR"},
        {"privacy_level": None},
        {"privacy_level": ["SELF_ONLY"]},
        {"tiktok_privacy_level": "SELF_ONLY"},
        {"privacyLevel": "SELF_ONLY"},
    ],
)
def test_guard_raises_on_unbranded_or_private_live_payload(mutation: dict[str, Any]) -> None:
    payload = {**_good_private(), "privacy_level": "PUBLIC_TO_EVERYONE", "brand_content_toggle": True, **mutation}
    with pytest.raises(ComplianceError):
        assert_tiktok_disclosure(payload, private=False)


def test_bad_private_payload_is_never_sent(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the payload builder were bypassed, `publish` checks the final form before any HTTP call."""
    import httpx
    import respx

    from clipforge.publish import upload_post

    video = tmp_path / "c.mp4"
    video.write_bytes(b"fake")
    bad = {**_good_private(), "request_id": "1", "brand_content_toggle": True}
    monkeypatch.setattr(upload_post, "build_payload", lambda *a, **k: bad)
    client = upload_post.UploadPostClient("k", wait_min_s=0, wait_max_s=0)
    req = _req(Platform.TIKTOK, PublishMode.PRIVATE).model_copy(update={"video_path": str(video)})
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{upload_post.BASE_URL}/upload").mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(ComplianceError):
            client.publish(req, campaign(), app_config)
    assert route.call_count == 0


def test_private_request_body_has_no_branded_field(tmp_path: Path, app_config: AppConfig) -> None:
    import httpx
    import respx

    from clipforge.publish import upload_post

    video = tmp_path / "c.mp4"
    video.write_bytes(b"fake")
    client = upload_post.UploadPostClient("k", wait_min_s=0, wait_max_s=0)
    req = _req(Platform.TIKTOK, PublishMode.PRIVATE).model_copy(update={"video_path": str(video)})
    with respx.mock() as router:
        route = router.post(f"{upload_post.BASE_URL}/upload").mock(
            return_value=httpx.Response(200, json={"success": True, "request_id": "1"})
        )
        assert client.publish(req, campaign(), app_config).accepted
    body = route.calls.last.request.content
    assert b'name="privacy_level"\r\n\r\nSELF_ONLY' in body
    assert b'name="brand_content_toggle"\r\n\r\nfalse' in body
    for fragment in (b"paid_partnership", b"disclose_commercial", b"branded_content", b"your_brand"):
        assert fragment not in body
