"""Invariant 8: no engagement automation anywhere in src/, and no engagement fields in any payload."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clipforge.compliance import ComplianceError, assert_no_engagement_fields
from clipforge.config import AppConfig
from clipforge.schemas import Platform, PublishMode, PublishRequest
from tests.compliance._helpers import campaign, xfail_until

SRC = Path(__file__).resolve().parents[2] / "src" / "clipforge"
ENFORCER = SRC / "compliance.py"  # holds the forbidden-name list itself

FORBIDDEN = [
    re.compile(r"first_comment", re.I),
    re.compile(
        r"\b(?:auto_?)?(?:like|follow|unfollow|comment|dm|direct_message|reply)_(?:post|user|video|tweet|media|"
        r"account|reel|short|story|creator)s?\b",
        re.I,
    ),
    re.compile(r"\b(?:send|post|create)_(?:dm|direct_message|comment|like|follow)s?\b", re.I),
    re.compile(r"""["'][^"']*/(?:likes?|follow(?:ers|ing|s)?|comments?|dms?|direct_messages?)(?:/|["'?])""", re.I),
]


def test_no_engagement_fields_or_endpoints() -> None:
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        if path == ENFORCER:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if any(p.search(line) for p in FORBIDDEN):
                hits.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")
    assert hits == []


@pytest.mark.parametrize(
    "line",
    [
        'payload["first_comment"] = "nice"',
        "def like_post(post_id): ...",
        "client.send_dm(user, text)",
        'httpx.post(f"{base}/v2/users/{uid}/follows")',
        'url = "https://api.example.com/media/1/comments"',
    ],
)
def test_static_patterns_catch_examples(line: str) -> None:
    assert any(p.search(line) for p in FORBIDDEN)


@pytest.mark.parametrize("line", ["await update.message.reply_text('ok')", "# a comment", "followup = 1"])
def test_static_patterns_allow_normal_code(line: str) -> None:
    assert not any(p.search(line) for p in FORBIDDEN)


@pytest.mark.parametrize(
    "payload",
    [
        {"first_comment": "x"},
        {"tiktok_first_comment": "x"},
        {"platform[]": ["tiktok"], "nested": {"X_First_Comment": "x"}},
        {"items": [{"like": True}]},
        {"follow": True},
    ],
)
def test_payload_engagement_fields_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ComplianceError):
        assert_no_engagement_fields(payload)


def test_payload_normal_fields_ok() -> None:
    assert_no_engagement_fields({"user": "gaming-1", "platform[]": ["tiktok"], "title": "#ad x", "async_upload": True})


@xfail_until("Phase 1 D: clipforge.publish.upload_post.build_payload")
@pytest.mark.parametrize("platform", list(Platform))
def test_upload_payload_has_no_engagement_fields(platform: Platform, app_config: AppConfig) -> None:
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
    assert_no_engagement_fields(build_payload(req, campaign(), app_config))
