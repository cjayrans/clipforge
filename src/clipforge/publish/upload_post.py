"""Upload-Post client: payload builder (compliance invariants 2, 4, 8, 9) and the HTTP client.

Docs: docs/ARCHITECTURE.md §4.7. Every external fact below was checked against the official docs on 2026-11-22.

Verified facts (page text, unless marked "summary" = the fetch tool summarised a long page, so treat it as likely but
unconfirmed; none of the summary-only items is load-bearing for a compliance rule):
- Upload: ``POST https://api.upload-post.com/api/upload`` (multipart/form-data in the examples), header
  ``Authorization: Apikey <key>``. https://docs.upload-post.com/api/upload-video
- Optional header ``Idempotency-Key`` (also ``X-Idempotency-Key`` / ``X-Request-Id``): a matching job is returned
  instead of a duplicate. We send ``Idempotency-Key`` = ``request_id`` = our post id.
- Common fields: ``user``, ``platform[]``, ``video`` (file), ``title``, ``request_id``, ``external_id`` (label only,
  max 255), ``async_upload``. Per-platform title overrides: ``tiktok_title``, ``instagram_title``, ``x_title``,
  ``youtube_title`` (each falls back to ``title``). ``description`` is used for LinkedIn/Facebook/YouTube/Pinterest
  only; ``youtube_description`` overrides it. TikTok and Instagram captions therefore travel in ``tiktok_title`` /
  ``instagram_title``. Uploads over 59 s are async regardless; we always send ``async_upload=true``.
- TikTok: ``privacy_level`` (PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, SELF_ONLY),
  ``post_mode`` (DIRECT_POST default), ``disable_inbox_fallback``, ``brand_content_toggle``, ``brand_organic_toggle``,
  ``is_aigc`` (DIRECT_POST only). Limits: 6 posts/min and 15/day per account.
- YouTube: ``youtube_title``/``title``, ``youtube_description``/``description``, ``categoryId`` (default "22"),
  ``privacyStatus`` (public|unlisted|private), ``selfDeclaredMadeForKids``, ``containsSyntheticMedia``,
  ``hasPaidProductPlacement``.
- Instagram: ``instagram_title``, ``media_type`` (REELS default), ``share_to_feed``, ``is_ai_generated``,
  ``collaborators`` (never sent). The API has no native paid-partnership label for Instagram Login accounts; the
  ``#ad`` caption prefix is the disclosure there.
  https://docs.upload-post.com/guides/paid-partnership
- X: ``x_title``, ``made_with_ai``, ``x_paid_partnership``.
- The API has auto-comment fields (listed in compliance.FORBIDDEN_PAYLOAD_FIELDS). We never send any (invariant 8).
- ``paid_partnership=true`` together with TikTok ``privacy_level=SELF_ONLY`` is refused with HTTP 400
  ``commercial_disclosure_privacy_conflict``. We do NOT send ``paid_partnership`` (the explicit
  ``brand_content_toggle`` always wins over it). Not documented: whether TikTok itself accepts
  ``brand_content_toggle=true`` with ``SELF_ONLY``; a private-mode TikTok post may be rejected (see PR notes).
- Status: ``GET https://api.upload-post.com/api/uploadposts/status?request_id=...`` (same auth header).
  https://docs.upload-post.com/api/upload-status. Top-level ``status``: pending, queued, processing, in_progress,
  completed, failed, not_found (404). Body has ``request_id``, ``external_id``, ``completed``, ``total``,
  ``last_update`` and a ``results`` ARRAY of ``{platform, success, message, upload_timestamp, ...}``; TikTok entries
  may carry ``post_url`` (null/"No Public URL" when sent to the inbox), ``platform_post_id``, ``fallback_to_inbox``.
  Per-platform status values: queued, processing, completed, failed, retryable, skipped.
  NOT stated by the docs: which key holds the URL in a status entry. We accept ``post_url`` or ``url`` and keep
  polling until one is present. The OpenAPI spec (https://docs.upload-post.com/openapi.json, 2.2 MB) could not be read
  in full, so the status response schema there is unverified.
- Async accept response (summary only): ``{"success": true, "message": ..., "request_id": ..., "total_platforms": n}``.

One Upload-Post request is made per (post, platform), so ``request_id`` = our post id is unique per request.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from clipforge.compliance import (
    AD_PREFIX,
    YOUTUBE_TITLE_LIMIT,
    assert_no_engagement_fields,
    normalize_caption,
)
from clipforge.config import AppConfig, Settings
from clipforge.schemas import CampaignIn, Platform, PublishMode, PublishRequest, PublishResult

log = logging.getLogger(__name__)

BASE_URL = "https://api.upload-post.com/api"
CONNECT_TIMEOUT_S = 10.0
REQUEST_TIMEOUT_S = 180.0  # a clip is at most 90 s of 1080x1920 video (tens of MB)
MAX_ATTEMPTS = 3


_LEADING_AD_RE = re.compile(r"^#ad(?![\w])[\s,.:;!|-]*", re.IGNORECASE)


class UploadPostError(RuntimeError):
    """Upload-Post could not be reached or answered with a server error after retries (outcome unknown)."""


# --------------------------------------------------------------------------------------------------
# Payload (pure)
# --------------------------------------------------------------------------------------------------


def _youtube_title(request: PublishRequest, caption: str) -> str:
    """YouTube title, always starting with '#ad ' (invariant 2 applies to every platform text), at most 100 chars."""
    raw = (request.youtube_title or "").strip()
    if not raw:
        raw = caption[len(AD_PREFIX) :].strip().split("\n", 1)[0]
    previous = None
    while previous != raw:  # drop any '#ad' the title already starts with, so it is not doubled
        previous = raw
        raw = _LEADING_AD_RE.sub("", raw, count=1).strip()
    title = f"{AD_PREFIX} {raw}".strip()
    if len(title) > YOUTUBE_TITLE_LIMIT:
        title = title[: YOUTUBE_TITLE_LIMIT - 1].rstrip() + "…"
    return title


def build_payload(request: PublishRequest, campaign: CampaignIn, app_config: AppConfig) -> dict[str, Any]:
    """The form fields (everything except the video file) for one platform of one post.

    Invariant 2: the caption goes through `compliance.normalize_caption` (starts with '#ad ', X has no URLs).
    Invariant 4: paid-content flags set, AI/synthetic flags false. Invariant 8: `assert_no_engagement_fields` on the
    final dict, and no auto-comment field is ever produced. Platform eligibility (invariant 9) is the dispatcher's
    job; this function only formats. Instagram has no private mode (PRD F9), so it is refused in PRIVATE mode here.
    """
    platform = request.platform
    if platform is Platform.INSTAGRAM and request.mode is PublishMode.PRIVATE:
        raise ValueError("instagram is skipped in private mode (PRD F9)")
    private = request.mode is PublishMode.PRIVATE
    caption = normalize_caption(request.caption, platform, campaign)
    post_id = str(request.post_id)

    payload: dict[str, Any] = {
        "user": request.upload_post_user,
        "platform[]": [platform.value],
        "request_id": post_id,
        "external_id": post_id,
        "async_upload": True,
    }
    if platform is Platform.TIKTOK:
        payload.update(
            {
                "title": caption,
                "tiktok_title": caption,
                "privacy_level": "SELF_ONLY" if private else "PUBLIC_TO_EVERYONE",
                "post_mode": "DIRECT_POST",
                "disable_inbox_fallback": True,
                "brand_content_toggle": True,
                "brand_organic_toggle": False,
                "is_aigc": False,
            }
        )
    elif platform is Platform.YOUTUBE:
        title = _youtube_title(request, caption)
        payload.update(
            {
                "title": title,
                "youtube_title": title,
                "description": caption,
                "youtube_description": caption,
                "categoryId": app_config.youtube.category_id,
                "privacyStatus": "private" if private else "public",
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": False,
                "hasPaidProductPlacement": True,
            }
        )
    elif platform is Platform.INSTAGRAM:
        payload.update(
            {
                "title": caption,
                "instagram_title": caption,
                "media_type": "REELS",
                "share_to_feed": True,
                "is_ai_generated": False,
            }
        )
    else:  # Platform.X
        payload.update(
            {
                "title": caption,
                "x_title": caption,
                "made_with_ai": False,
                "x_paid_partnership": True,
            }
        )
    assert_no_engagement_fields(payload)
    return payload


def _form_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return [_form_value(v) for v in value]
    return str(value) if not isinstance(value, str) else value


def fake_dry_run_url(platform: Platform, post_id: int) -> str:
    """dry_run makes no HTTP call; this URL can never resolve (`.invalid` is reserved)."""
    return f"https://example.invalid/{platform.value}/{post_id}"


# --------------------------------------------------------------------------------------------------
# Status parsing (pure)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlatformStatus:
    platform: str
    success: bool | None = None
    status: str | None = None
    message: str = ""
    post_url: str | None = None
    fallback_to_inbox: bool = False
    skipped: bool = False
    upload_timestamp: datetime | None = None


@dataclass(frozen=True)
class UploadStatus:
    request_id: str | None
    status: str
    message: str = ""
    results: list[PlatformStatus] = field(default_factory=list)


@dataclass(frozen=True)
class StatusOutcome:
    kind: Literal["pending", "live", "failed"]
    post_url: str | None = None
    published_at: datetime | None = None
    error: str | None = None
    # True when Upload-Post reports success for the platform but gave no URL (yet).
    published_without_url: bool = False


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _entry(platform: str, raw: Mapping[str, Any]) -> PlatformStatus:
    url = raw.get("post_url") or raw.get("url")
    success = raw.get("success")
    return PlatformStatus(
        platform=str(raw.get("platform") or platform),
        success=success if isinstance(success, bool) else None,
        status=str(raw["status"]) if raw.get("status") is not None else None,
        message=str(raw.get("message") or raw.get("error") or ""),
        post_url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
        fallback_to_inbox=bool(raw.get("fallback_to_inbox")),
        skipped=bool(raw.get("skipped")) or raw.get("status") == "skipped",
        upload_timestamp=_parse_ts(raw.get("upload_timestamp")),
    )


def parse_status(body: Mapping[str, Any]) -> UploadStatus:
    """Tolerant parse: `results` is an array in the status docs, but a platform-keyed object is accepted too."""
    raw_results = body.get("results")
    results: list[PlatformStatus] = []
    if isinstance(raw_results, list):
        results = [_entry("", r) for r in raw_results if isinstance(r, Mapping)]
    elif isinstance(raw_results, Mapping):
        results = [_entry(str(k), v) for k, v in raw_results.items() if isinstance(v, Mapping)]
    return UploadStatus(
        request_id=str(body["request_id"]) if body.get("request_id") is not None else None,
        status=str(body.get("status") or "pending"),
        message=str(body.get("message") or ""),
        results=results,
    )


def interpret_status(status: UploadStatus, platform: Platform) -> StatusOutcome:
    """Turn a status response into pending / live / failed for one platform."""
    entry = next((r for r in status.results if r.platform.lower() == platform.value), None)
    if entry is not None:
        if entry.skipped:
            return StatusOutcome(
                "failed", error=f"skipped by Upload-Post: {entry.message or 'platform not configured'}"
            )
        if entry.fallback_to_inbox:
            return StatusOutcome("failed", error="sent to the TikTok inbox instead of being published")
        if entry.success is False and entry.status != "retryable":
            return StatusOutcome("failed", error=entry.message or "platform reported failure")
        if entry.post_url and entry.success is not False and entry.status not in ("failed",):
            return StatusOutcome("live", post_url=entry.post_url, published_at=entry.upload_timestamp)
        if entry.status == "failed":
            return StatusOutcome("failed", error=entry.message or "platform reported failure")
    if status.status == "failed":
        return StatusOutcome("failed", error=status.message or "Upload-Post reported the upload failed")
    published_without_url = bool(entry and entry.success is True and entry.post_url is None)
    return StatusOutcome("pending", published_without_url=published_without_url)


# --------------------------------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------------------------------


class _Retryable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, Mapping):
        parts = [str(body[k]) for k in ("error_code", "message", "error") if body.get(k)]
        if parts:
            return f"HTTP {response.status_code}: " + "; ".join(parts)
    return f"HTTP {response.status_code}"


class UploadPostClient:
    """The one Upload-Post client. `dry_run` requests never touch the network."""

    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = BASE_URL,
        http: httpx.Client | None = None,
        wait_min_s: float = 1.0,
        wait_max_s: float = 8.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=httpx.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        self._wait_min_s = wait_min_s
        self._wait_max_s = wait_max_s

    @classmethod
    def from_settings(cls, settings: Settings) -> UploadPostClient:
        key = settings.secrets.upload_post_api_key
        return cls(key.get_secret_value() if key is not None else None)

    def close(self) -> None:
        self._http.close()

    def _headers(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        if not self._api_key:
            raise UploadPostError("UPLOAD_POST_API_KEY is not set")
        return {"Authorization": f"Apikey {self._api_key}", **(extra or {})}

    def _retrying(self) -> Retrying:
        return Retrying(
            retry=retry_if_exception_type(_Retryable),
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=wait_exponential(multiplier=self._wait_min_s, max=self._wait_max_s),
            reraise=True,
        )

    def _send(self, make_request: Any, what: str) -> httpx.Response:
        """Run one HTTP call with retries on transport errors, HTTP 429 and 5xx. Other statuses are returned."""
        try:
            for attempt in self._retrying():
                with attempt:
                    try:
                        response: httpx.Response = make_request()
                    except httpx.TransportError as exc:
                        raise _Retryable(f"{what}: {type(exc).__name__}") from exc
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _Retryable(f"{what}: HTTP {response.status_code}")
        except _Retryable as exc:
            raise UploadPostError(f"{exc.reason} (after {MAX_ATTEMPTS} attempts)") from None
        return response

    def publish(self, request: PublishRequest, campaign: CampaignIn, app_config: AppConfig) -> PublishResult:
        """Send one post to one platform. Idempotent by `request_id` (= post id).

        Returns `accepted=False` with an error for a definitive rejection (4xx). Raises `UploadPostError` when the
        outcome is unknown (network failure / 5xx after retries); the caller leaves the post PUBLISHING and retries
        with the same request id.
        """
        payload = build_payload(request, campaign, app_config)  # validates (#ad, flags, no engagement) even in dry_run
        if request.mode is PublishMode.DRY_RUN:
            return PublishResult(
                post_id=request.post_id,
                platform=request.platform,
                accepted=True,
                post_url=fake_dry_run_url(request.platform, request.post_id),
                published_at=datetime.now(UTC),
                upload_post_ref=f"dry-run-{request.post_id}",
            )
        form = {k: _form_value(v) for k, v in payload.items()}
        video = Path(request.video_path)
        headers = self._headers({"Idempotency-Key": str(payload["request_id"])})

        def call() -> httpx.Response:
            with video.open("rb") as fh:
                return self._http.post(
                    f"{self._base_url}/upload",
                    headers=headers,
                    data=form,
                    files={"video": (video.name, fh, "video/mp4")},
                )

        response = self._send(call, "upload")
        if response.status_code not in (200, 202):
            return PublishResult(
                post_id=request.post_id,
                platform=request.platform,
                accepted=False,
                error=_error_text(response),
            )
        warnings: list[str] = []
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, Mapping):
            if body.get("success") is False:
                return PublishResult(
                    post_id=request.post_id,
                    platform=request.platform,
                    accepted=False,
                    error=_error_text(response),
                )
            raw_warnings = body.get("warnings")
            if isinstance(raw_warnings, list):
                warnings = [str(w) for w in raw_warnings]
        log.info("upload-post accepted post %s (%s)", request.post_id, request.platform.value)
        return PublishResult(
            post_id=request.post_id,
            platform=request.platform,
            accepted=True,
            upload_post_ref=str(payload["request_id"]),
            warnings=warnings,
        )

    def get_status(self, request_id: str) -> UploadStatus:
        """Poll the status of an async upload. A 404 `not_found` is returned as status "not_found"."""
        headers = self._headers()
        response = self._send(
            lambda: self._http.get(
                f"{self._base_url}/uploadposts/status", headers=headers, params={"request_id": request_id}
            ),
            "status",
        )
        if response.status_code == 404:
            return UploadStatus(request_id=request_id, status="not_found")
        if response.status_code != 200:
            raise UploadPostError(f"status: {_error_text(response)}")
        try:
            body = response.json()
        except ValueError as exc:
            raise UploadPostError("status: response was not JSON") from exc
        if not isinstance(body, Mapping):
            raise UploadPostError("status: unexpected response shape")
        return parse_status(body)
