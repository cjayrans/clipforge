"""D5: YouTube view snapshots. The Data API is mocked with respx."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings, load_settings
from clipforge.db import ViewSnapshotRow
from clipforge.schemas import Platform
from clipforge.track.youtube_views import (
    API_URL,
    BATCH,
    YouTubeClient,
    YouTubeError,
    YouTubeQuotaExceeded,
    snapshot_youtube_views,
    video_id_from_url,
)
from tests.support.factories import Factory
from tests.track.helpers import T0, real_post

KEY = "yt-key-secret-value"
NOW = T0 + timedelta(days=2)


def items(**counts: object) -> dict[str, object]:
    return {"items": [{"id": vid, "statistics": {"viewCount": views}} for vid, views in counts.items()]}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/shorts/abcDEF12345", "abcDEF12345"),
        ("https://youtube.com/watch?v=abcDEF12345&t=3", "abcDEF12345"),
        ("https://m.youtube.com/watch?v=abcDEF12345", "abcDEF12345"),
        ("https://youtu.be/abcDEF12345?si=x", "abcDEF12345"),
        ("https://www.youtube.com/embed/abcDEF12345", "abcDEF12345"),
        ("https://example.invalid/youtube/12", None),
        ("https://www.tiktok.com/@u/video/1", None),
        ("https://www.youtube.com/@channel", None),
        ("https://www.youtube.com/watch", None),
        ("https://youtu.be/", None),
        ("https://evil.example/youtube.com/shorts/abcDEF12345", None),
        (None, None),
    ],
)
def test_video_id_from_url(url: str | None, expected: str | None) -> None:
    assert video_id_from_url(url) == expected


def test_fetch_uses_documented_request_and_parses_both_count_types() -> None:
    client = YouTubeClient(KEY)
    with respx.mock() as router:
        route = router.get(API_URL).mock(
            return_value=httpx.Response(200, json=items(aaaaaa="1234", bbbbbb=99, cccccc="x"))
        )
        counts = client.fetch_view_counts(["aaaaaa", "bbbbbb", "cccccc", "dddddd"])
    assert counts == {"aaaaaa": 1234, "bbbbbb": 99}  # junk and omitted ids are absent
    params = route.calls.last.request.url.params
    assert params["part"] == "statistics"
    assert params["id"] == "aaaaaa,bbbbbb,cccccc,dddddd"
    assert params["key"] == KEY


def test_ids_are_batched_and_deduplicated() -> None:
    client = YouTubeClient(KEY)
    ids = [f"vid{i:05d}" for i in range(BATCH * 2 + 20)]
    with respx.mock() as router:
        route = router.get(API_URL).mock(return_value=httpx.Response(200, json={"items": []}))
        client.fetch_view_counts(ids + ids[:5])
    sizes = [len(c.request.url.params["id"].split(",")) for c in route.calls]
    assert sizes == [BATCH, BATCH, 20]


def test_transient_errors_are_retried() -> None:
    client = YouTubeClient(KEY, wait_min_s=0)
    with respx.mock() as router:
        route = router.get(API_URL).mock(
            side_effect=[httpx.Response(503), httpx.ConnectError("x"), httpx.Response(200, json=items(aaaaaa="5"))]
        )
        assert client.fetch_view_counts(["aaaaaa"]) == {"aaaaaa": 5}
    assert route.call_count == 3


def test_exhausted_retries_raise_without_the_key() -> None:
    client = YouTubeClient(KEY, wait_min_s=0)
    with respx.mock() as router:
        router.get(API_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(YouTubeError) as exc:
            client.fetch_view_counts(["aaaaaa"])
    assert KEY not in str(exc.value)


def test_quota_exceeded_and_other_errors() -> None:
    client = YouTubeClient(KEY, wait_min_s=0)
    quota = {"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}
    with respx.mock() as router:
        router.get(API_URL).mock(return_value=httpx.Response(403, json=quota))
        with pytest.raises(YouTubeQuotaExceeded):
            client.fetch_view_counts(["aaaaaa"])
    with respx.mock() as router:
        router.get(API_URL).mock(
            return_value=httpx.Response(400, json={"error": {"errors": [{"reason": "badRequest"}]}})
        )
        with pytest.raises(YouTubeError, match="400 badRequest") as exc:
            client.fetch_view_counts(["aaaaaa"])
    assert KEY not in str(exc.value)


# --- snapshots ------------------------------------------------------------------------------------------------------


def yt_post(factory: Factory, session: Session, vid: str, *, age_days: float = 1.0, url: str | None = None) -> int:
    post = real_post(
        factory,
        session,
        platform=Platform.YOUTUBE,
        published=NOW - timedelta(days=age_days),
        url=url or f"https://www.youtube.com/shorts/{vid}",
    )
    return post.id


def test_snapshot_writes_one_row_per_tracked_post(factory: Factory, session: Session, settings: Settings) -> None:
    keep = yt_post(factory, session, "aaaaaa")
    yt_post(factory, session, "oldold", age_days=31)  # beyond the 30 day tracking window
    yt_post(factory, session, "fake01", url="https://example.invalid/youtube/5")
    real_post(factory, session, platform=Platform.TIKTOK)  # other platform
    client = YouTubeClient(KEY, wait_min_s=0)
    with respx.mock() as router:
        route = router.get(API_URL).mock(return_value=httpx.Response(200, json=items(aaaaaa="321", oldold="9")))
        assert snapshot_youtube_views(session, settings, NOW, client=client) == 1
    assert route.calls.last.request.url.params["id"] == "aaaaaa"
    snap = session.scalars(select(ViewSnapshotRow)).one()
    assert (snap.post_id, snap.views, snap.source, snap.taken_at) == (keep, 321, "youtube_api", NOW)


def test_no_key_means_no_http_and_no_rows(
    factory: Factory, session: Session, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    yt_post(factory, session, "aaaaaa")
    with respx.mock(assert_all_called=False) as router:
        assert snapshot_youtube_views(session, settings, NOW) == 0
    assert router.calls.call_count == 0


def test_key_from_settings_is_used(factory: Factory, session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    yt_post(factory, session, "aaaaaa")
    with respx.mock() as router:
        route = router.get(API_URL).mock(return_value=httpx.Response(200, json=items(aaaaaa="7")))
        assert snapshot_youtube_views(session, load_settings(), NOW) == 1
    assert route.calls.last.request.url.params["key"] == KEY


def test_quota_exhaustion_is_not_fatal(factory: Factory, session: Session, settings: Settings) -> None:
    yt_post(factory, session, "aaaaaa")
    client = YouTubeClient(KEY, wait_min_s=0)
    quota = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    with respx.mock() as router:
        router.get(API_URL).mock(return_value=httpx.Response(403, json=quota))
        assert snapshot_youtube_views(session, settings, NOW, client=client) == 0


def test_nothing_to_do_makes_no_call(session: Session, settings: Settings) -> None:
    client = YouTubeClient(KEY)
    with respx.mock(assert_all_called=False) as router:
        assert snapshot_youtube_views(session, settings, NOW, client=client) == 0
    assert router.calls.call_count == 0
