"""Daily YouTube view snapshots via the Data API `videos.list` (PRD F11), only when `YOUTUBE_API_KEY` is set.

Verified against https://developers.google.com/youtube/v3/docs/videos/list (2026-11-22): `GET
https://www.googleapis.com/youtube/v3/videos` with `part=statistics` and `id=<comma-separated ids>`; quota cost 1 unit
per call (default 10,000 units/day); public statistics need only an API key (no OAuth). NOT stated on the pages we could
read: the exact name of the key parameter (we use the standard `key`), a maximum number of ids per call (we batch at 50,
the documented limit of the API in practice), the wire type of `statistics.viewCount` (the example shows a string, the
property table says unsigned long: we accept a string or a number), and whether private/deleted ids are omitted from
`items` (we tolerate ids missing from the response: they simply get no snapshot).

Content Rewards pays on its own verified views, which we cannot read; these snapshots feed weekly learning only.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from clipforge.config import Settings
from clipforge.db import PostRow, ViewSnapshotRow
from clipforge.schemas import Platform, PostState
from clipforge.track.metrics import is_fake_url

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/youtube/v3/videos"
BATCH = 50
MAX_ATTEMPTS = 3
TRACK_DAYS = 30  # stop snapshotting a video a month after it went live

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")


class YouTubeError(RuntimeError):
    pass


class YouTubeQuotaExceeded(YouTubeError):
    pass


class _Retryable(Exception):
    pass


def video_id_from_url(url: str | None) -> str | None:
    """The video id of a youtube.com/shorts/<id>, /watch?v=<id>, /embed/<id> or youtu.be/<id> URL; None otherwise."""
    if not url or is_fake_url(url):
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    candidate: str | None = None
    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        segments = [s for s in parts.path.split("/") if s]
        if segments and segments[0] in ("shorts", "embed", "live", "v") and len(segments) > 1:
            candidate = segments[1]
        elif segments and segments[0] == "watch":
            candidate = (parse_qs(parts.query).get("v") or [None])[0]
    return candidate if candidate and _ID_RE.match(candidate) else None


def _parse_views(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


class YouTubeClient:
    def __init__(self, api_key: str, *, http: httpx.Client | None = None, wait_min_s: float = 1.0) -> None:
        self._key = api_key
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._wait = wait_min_s

    @classmethod
    def from_settings(cls, settings: Settings) -> YouTubeClient | None:
        key = settings.secrets.youtube_api_key
        return cls(key.get_secret_value()) if key is not None else None

    def close(self) -> None:
        self._http.close()

    def _get(self, ids: list[str]) -> httpx.Response:
        params = {"part": "statistics", "id": ",".join(ids), "key": self._key}
        try:
            for attempt in Retrying(
                retry=retry_if_exception_type(_Retryable),
                stop=stop_after_attempt(MAX_ATTEMPTS),
                wait=wait_exponential(multiplier=self._wait, max=10),
                reraise=True,
            ):
                with attempt:
                    try:
                        response = self._http.get(API_URL, params=params)
                    except httpx.TransportError as exc:
                        raise _Retryable(type(exc).__name__) from exc
                    if response.status_code >= 500:
                        raise _Retryable(f"HTTP {response.status_code}")
        except _Retryable as exc:
            raise YouTubeError(f"YouTube unreachable after {MAX_ATTEMPTS} attempts ({exc})") from None
        return response

    def fetch_view_counts(self, video_ids: list[str]) -> dict[str, int]:
        """View counts for the ids YouTube returned (omitted ids are absent). The API key never appears in errors."""
        counts: dict[str, int] = {}
        unique = list(dict.fromkeys(video_ids))
        for start in range(0, len(unique), BATCH):
            response = self._get(unique[start : start + BATCH])
            if response.status_code != 200:
                reasons = _error_reasons(response)
                if "quotaExceeded" in reasons:
                    raise YouTubeQuotaExceeded("YouTube Data API quota exceeded")
                raise YouTubeError(f"YouTube returned HTTP {response.status_code} {','.join(reasons)}".strip())
            body = response.json()
            for item in body.get("items", []) if isinstance(body, Mapping) else []:
                views = _parse_views((item.get("statistics") or {}).get("viewCount"))
                if isinstance(item.get("id"), str) and views is not None:
                    counts[item["id"]] = views
        return counts


def _error_reasons(response: httpx.Response) -> list[str]:
    try:
        errors = response.json().get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return []
    return [str(e.get("reason")) for e in errors if isinstance(e, Mapping) and e.get("reason")]


def snapshot_youtube_views(
    session: Session, settings: Settings, now: datetime, *, client: YouTubeClient | None = None
) -> int:
    """Record one `view_snapshots` row per live YouTube post tracked in the last 30 days. Returns rows written.

    Does nothing (returns 0) without an API key. Quota exhaustion is logged and skipped, never fatal.
    """
    active = client if client is not None else YouTubeClient.from_settings(settings)
    if active is None:
        return 0
    cutoff = now - timedelta(days=TRACK_DAYS)
    posts = session.scalars(
        select(PostRow).where(PostRow.platform == Platform.YOUTUBE, PostRow.state == PostState.LIVE)
    ).all()
    by_video: dict[str, list[PostRow]] = {}
    for post in posts:
        video_id = video_id_from_url(post.post_url)
        if video_id and (post.published_at is None or post.published_at >= cutoff):
            by_video.setdefault(video_id, []).append(post)
    if not by_video:
        return 0
    try:
        counts = active.fetch_view_counts(list(by_video))
    except YouTubeQuotaExceeded:
        log.warning("YouTube quota exceeded; no snapshots today")
        return 0
    written = 0
    for video_id, views in counts.items():
        for post in by_video.get(video_id, []):
            session.add(ViewSnapshotRow(post_id=post.id, taken_at=now, views=views, source="youtube_api"))
            written += 1
    session.flush()
    return written
