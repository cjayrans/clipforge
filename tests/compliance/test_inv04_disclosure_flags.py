"""Invariant 4: paid-content flags always set, AI/synthetic flags false; no synthetic faces/voices."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clipforge.config import AppConfig
from clipforge.schemas import Platform, PublishMode, PublishRequest
from tests.compliance._helpers import campaign, xfail_until

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


@xfail_until("Phase 1 D: clipforge.publish.upload_post.build_payload")
@pytest.mark.parametrize("platform", list(Platform))
def test_disclosure_flags_per_platform(platform: Platform, app_config: AppConfig) -> None:
    from clipforge.publish.upload_post import build_payload  # type: ignore[attr-defined]

    req = PublishRequest(
        post_id=1,
        clip_id=1,
        platform=platform,
        upload_post_user="gaming-1",
        video_path="/tmp/c.mp4",
        caption="#ad clip",
        youtube_title="A title",
        mode=PublishMode.DRY_RUN,
    )
    p = build_payload(req, campaign(), app_config)
    if platform is Platform.TIKTOK:
        assert _truthy(p["brand_content_toggle"])
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
