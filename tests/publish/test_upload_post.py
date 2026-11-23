"""D4: Upload-Post payload builder and client. External HTTP is mocked with respx (never real network)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from clipforge.compliance import ComplianceError, assert_no_engagement_fields, contains_url
from clipforge.config import AppConfig
from clipforge.publish.upload_post import (
    BASE_URL,
    UploadPostClient,
    UploadPostError,
    build_payload,
    fake_dry_run_url,
    interpret_status,
    parse_status,
)
from clipforge.schemas import Platform, PublishMode, PublishRequest
from tests.compliance._helpers import campaign

API_KEY = "test-key-do-not-log-1234567890"


def _req(platform: Platform = Platform.TIKTOK, mode: PublishMode = PublishMode.LIVE, **kw: Any) -> PublishRequest:
    data: dict[str, Any] = {
        "post_id": 42,
        "clip_id": 7,
        "platform": platform,
        "upload_post_user": "gaming-1",
        "video_path": "/tmp/none.mp4",
        "caption": "#ad the heist went sideways",
        "youtube_title": "Heist goes wrong",
        "mode": mode,
    }
    data.update(kw)
    return PublishRequest(**data)


def _truthy(v: object) -> bool:
    return str(v).lower() == "true"


@pytest.fixture
def video(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42 fake video bytes")
    return p


@pytest.fixture
def client() -> UploadPostClient:
    return UploadPostClient(API_KEY, wait_min_s=0, wait_max_s=0)


# --- payload ------------------------------------------------------------------------------------------


def test_tiktok_payload_live(app_config: AppConfig) -> None:
    p = build_payload(_req(Platform.TIKTOK), campaign(required_tags=["#GTA6"]), app_config)
    assert p["user"] == "gaming-1"
    assert p["platform[]"] == ["tiktok"]
    assert p["request_id"] == "42"
    assert p["external_id"] == "42"
    assert p["async_upload"] is True
    assert p["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert p["post_mode"] == "DIRECT_POST"
    assert p["disable_inbox_fallback"] is True
    assert p["brand_content_toggle"] is True
    assert p["brand_organic_toggle"] is False
    assert p["is_aigc"] is False
    assert p["tiktok_title"] == "#ad the heist went sideways #GTA6"
    assert "paid_partnership" not in p  # would conflict with SELF_ONLY; brand_content_toggle is explicit


def test_private_mode_values(app_config: AppConfig) -> None:
    tt = build_payload(_req(Platform.TIKTOK, PublishMode.PRIVATE), campaign(), app_config)
    assert tt["privacy_level"] == "SELF_ONLY"
    assert tt["brand_content_toggle"] is True  # invariant 4 holds in every mode
    yt = build_payload(_req(Platform.YOUTUBE, PublishMode.PRIVATE), campaign(), app_config)
    assert yt["privacyStatus"] == "private"
    assert yt["hasPaidProductPlacement"] is True


def test_instagram_refused_in_private_mode(app_config: AppConfig) -> None:
    with pytest.raises(ValueError, match="private mode"):
        build_payload(_req(Platform.INSTAGRAM, PublishMode.PRIVATE), campaign(), app_config)


def test_youtube_payload(app_config: AppConfig) -> None:
    p = build_payload(_req(Platform.YOUTUBE), campaign(), app_config)
    assert p["youtube_description"].startswith("#ad ")
    assert p["youtube_title"].startswith("#ad ")
    assert p["title"] == p["youtube_title"]
    assert p["categoryId"] == "22"
    assert p["privacyStatus"] == "public"
    assert p["hasPaidProductPlacement"] is True
    assert p["containsSyntheticMedia"] is False
    assert p["selfDeclaredMadeForKids"] is False


def test_youtube_title_rules(app_config: AppConfig) -> None:
    long = _req(Platform.YOUTUBE, youtube_title="#ad " + "word " * 60)
    title = build_payload(long, campaign(), app_config)["youtube_title"]
    assert title.startswith("#ad ")
    assert not title.startswith("#ad #ad")
    assert len(title) <= 100
    missing = _req(Platform.YOUTUBE, youtube_title=None)
    assert build_payload(missing, campaign(), app_config)["youtube_title"] == "#ad the heist went sideways"


def test_instagram_and_x_payload(app_config: AppConfig) -> None:
    ig = build_payload(_req(Platform.INSTAGRAM), campaign(), app_config)
    assert ig["media_type"] == "REELS"
    assert ig["share_to_feed"] is True
    assert ig["is_ai_generated"] is False
    assert "collaborators" not in ig
    x = build_payload(_req(Platform.X, caption="#ad watch https://twitch.tv/x now"), campaign(), app_config)
    assert x["made_with_ai"] is False
    assert not contains_url(x["x_title"])
    assert not contains_url(x["title"])


@pytest.mark.parametrize("platform", list(Platform))
@pytest.mark.parametrize("raw", ["no disclosure here", "#AD, loud", "#ad #ad twice", ""])
def test_every_caption_starts_with_ad(platform: Platform, raw: str, app_config: AppConfig) -> None:
    c = campaign(required_tags=["#GTA6"])
    p = build_payload(_req(platform, caption=raw), c, app_config)
    keys = ("youtube_description",) if platform is Platform.YOUTUBE else (f"{platform.value}_title", "title")
    for k in keys:
        assert p[k].startswith("#ad "), (k, p[k])


@pytest.mark.parametrize("platform", list(Platform))
@pytest.mark.parametrize("mode", [PublishMode.DRY_RUN, PublishMode.LIVE])
def test_no_engagement_fields_in_any_payload(platform: Platform, mode: PublishMode, app_config: AppConfig) -> None:
    p = build_payload(_req(platform, mode), campaign(), app_config)
    assert_no_engagement_fields(p)
    assert not any("comment" in k for k in p)


def test_empty_caption_with_nothing_else_is_refused(app_config: AppConfig) -> None:
    with pytest.raises(ComplianceError):
        build_payload(_req(caption="#ad"), campaign(disclosure_text="#ad"), app_config)


# --- client -------------------------------------------------------------------------------------------


def test_dry_run_makes_no_http_call(video: Path, app_config: AppConfig) -> None:
    keyless = UploadPostClient(None)
    with respx.mock(assert_all_called=False) as router:
        result = keyless.publish(_req(mode=PublishMode.DRY_RUN, video_path=str(video)), campaign(), app_config)
    assert router.calls.call_count == 0
    assert result.accepted
    assert result.post_url == fake_dry_run_url(Platform.TIKTOK, 42) == "https://example.invalid/tiktok/42"


def test_publish_sends_expected_request(video: Path, app_config: AppConfig, client: UploadPostClient) -> None:
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/upload").mock(
            return_value=httpx.Response(200, json={"success": True, "request_id": "42", "total_platforms": 1})
        )
        result = client.publish(_req(video_path=str(video)), campaign(), app_config)
    assert result.accepted
    assert result.upload_post_ref == "42"
    assert result.post_url is None
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Apikey {API_KEY}"
    assert req.headers["Idempotency-Key"] == "42"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content
    for needle in (
        b'name="user"\r\n\r\ngaming-1',
        b'name="platform[]"\r\n\r\ntiktok',
        b'name="request_id"\r\n\r\n42',
        b'name="async_upload"\r\n\r\ntrue',
        b'name="brand_content_toggle"\r\n\r\ntrue',
        b'name="brand_organic_toggle"\r\n\r\nfalse',
        b'name="is_aigc"\r\n\r\nfalse',
        b'name="video"; filename="clip.mp4"',
        b"fake video bytes",
    ):
        assert needle in body, needle
    assert b"first_comment" not in body


def test_transient_errors_are_retried_with_same_request_id(
    video: Path, app_config: AppConfig, client: UploadPostClient
) -> None:
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/upload").mock(
            side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, json={"success": True})]
        )
        result = client.publish(_req(video_path=str(video)), campaign(), app_config)
    assert result.accepted
    assert route.call_count == 3
    assert {c.request.headers["Idempotency-Key"] for c in route.calls} == {"42"}


def test_exhausted_retries_raise_without_leaking_the_key(
    video: Path, app_config: AppConfig, client: UploadPostClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(500, text="oops"))
        with pytest.raises(UploadPostError) as exc:
            client.publish(_req(video_path=str(video)), campaign(), app_config)
    assert route.call_count == 3
    assert API_KEY not in str(exc.value)
    assert API_KEY not in caplog.text


def test_4xx_is_not_retried_and_returns_a_rejection(
    video: Path, app_config: AppConfig, client: UploadPostClient
) -> None:
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/upload").mock(
            return_value=httpx.Response(
                400, json={"success": False, "message": "bad", "error_code": "commercial_disclosure_privacy_conflict"}
            )
        )
        result = client.publish(_req(video_path=str(video)), campaign(), app_config)
    assert route.call_count == 1
    assert not result.accepted
    assert result.error is not None
    assert "commercial_disclosure_privacy_conflict" in result.error
    assert API_KEY not in result.error


def test_success_false_body_is_a_rejection(video: Path, app_config: AppConfig, client: UploadPostClient) -> None:
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(
            return_value=httpx.Response(200, json={"success": False, "message": "no"})
        )
        result = client.publish(_req(video_path=str(video)), campaign(), app_config)
    assert not result.accepted


def test_missing_api_key_refuses_a_real_publish(video: Path, app_config: AppConfig) -> None:
    with respx.mock(assert_all_called=False), pytest.raises(UploadPostError, match="UPLOAD_POST_API_KEY"):
        UploadPostClient(None).publish(_req(video_path=str(video)), campaign(), app_config)


def test_get_status_calls_documented_endpoint(client: UploadPostClient) -> None:
    body = {
        "request_id": "42",
        "status": "completed",
        "completed": 1,
        "total": 1,
        "results": [
            {
                "platform": "tiktok",
                "success": True,
                "message": "Published",
                "upload_timestamp": "2026-11-24T01:10:30Z",
                "post_url": "https://www.tiktok.com/@u/video/1",
            }
        ],
    }
    with respx.mock() as router:
        route = router.get(f"{BASE_URL}/uploadposts/status", params={"request_id": "42"}).mock(
            return_value=httpx.Response(200, json=body)
        )
        status = client.get_status("42")
    assert route.calls.last.request.headers["Authorization"] == f"Apikey {API_KEY}"
    outcome = interpret_status(status, Platform.TIKTOK)
    assert outcome.kind == "live"
    assert outcome.post_url == "https://www.tiktok.com/@u/video/1"
    assert outcome.published_at == datetime(2026, 11, 24, 1, 10, 30, tzinfo=UTC)


def test_get_status_not_found(client: UploadPostClient) -> None:
    with respx.mock() as router:
        router.get(f"{BASE_URL}/uploadposts/status").mock(
            return_value=httpx.Response(404, json={"status": "not_found", "request_id": "9"})
        )
        assert client.get_status("9").status == "not_found"


# --- status interpretation (pure) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        ({"status": "pending"}, "pending"),
        (
            {"status": "in_progress", "results": [{"platform": "tiktok", "success": True, "message": "Queued"}]},
            "pending",
        ),
        ({"status": "failed", "message": "no activity", "results": []}, "failed"),
        (
            {"status": "completed", "results": [{"platform": "tiktok", "success": False, "message": "rejected"}]},
            "failed",
        ),
        (
            {"status": "completed", "results": [{"platform": "tiktok", "success": True, "fallback_to_inbox": True}]},
            "failed",
        ),
        ({"status": "completed", "results": [{"platform": "tiktok", "skipped": True, "message": "x"}]}, "failed"),
        (
            {"status": "processing", "results": [{"platform": "tiktok", "status": "retryable", "success": False}]},
            "pending",
        ),
        # platform-keyed results (sync-style) with `url`
        ({"status": "completed", "results": {"tiktok": {"success": True, "url": "https://t.example/1"}}}, "live"),
        # another platform's result must not count for ours
        (
            {"status": "completed", "results": [{"platform": "instagram", "success": True, "post_url": "https://i/1"}]},
            "pending",
        ),
    ],
)
def test_interpret_status(body: dict[str, Any], kind: str) -> None:
    assert interpret_status(parse_status(body), Platform.TIKTOK).kind == kind


def test_success_without_url_is_flagged() -> None:
    out = interpret_status(
        parse_status({"status": "completed", "results": [{"platform": "tiktok", "success": True}]}), Platform.TIKTOK
    )
    assert out.kind == "pending"
    assert out.published_without_url


def test_non_http_urls_are_ignored() -> None:
    out = interpret_status(
        parse_status(
            {"status": "completed", "results": [{"platform": "tiktok", "success": True, "post_url": "No Public URL"}]}
        ),
        Platform.TIKTOK,
    )
    assert out.kind == "pending"
