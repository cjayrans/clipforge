"""Invariant 1: download only URLs registered on an active campaign; Twitch needs permission; YouTube/other never."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from clipforge.compliance import ComplianceError, assert_registered_source, classify_source_host
from clipforge.config import Settings
from clipforge.schemas import CampaignStatus, SourceHost
from tests.compliance._helpers import campaign
from tests.support.factories import Factory

DIRECT = "https://cdn.example.com/campaign/ep1.mp4"


def test_unregistered_source_refused() -> None:
    c = campaign()
    with pytest.raises(ComplianceError, match="not registered"):
        assert_registered_source(DIRECT, c, [])
    with pytest.raises(ComplianceError, match="not registered"):
        assert_registered_source(DIRECT, c, ["https://cdn.example.com/campaign/ep2.mp4"])
    assert assert_registered_source(DIRECT, c, [DIRECT]) is SourceHost.DIRECT
    # Normalization: host case and fragments don't matter; the query does.
    assert assert_registered_source("https://CDN.example.com/campaign/ep1.mp4#t=5", c, [DIRECT]) is SourceHost.DIRECT
    with pytest.raises(ComplianceError):
        assert_registered_source(DIRECT + "?v=2", c, [DIRECT])


@pytest.mark.parametrize("status", [CampaignStatus.DRAINED, CampaignStatus.ENDED])
def test_inactive_campaign_refused(status: CampaignStatus) -> None:
    with pytest.raises(ComplianceError, match="not active"):
        assert_registered_source(DIRECT, campaign(status=status), [DIRECT])


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://m.youtube.com/live/abc",
        "https://music.youtube.com/watch?v=abc",
        "https://www.youtube-nocookie.com/embed/abc",
        "https://kick.com/someone/videos/1",
    ],
)
def test_youtube_host_refused(url: str) -> None:
    c = campaign(allow_stream_download=True)  # even with stream permission and registration
    with pytest.raises(ComplianceError, match="never downloaded"):
        assert_registered_source(url, c, [url])


@pytest.mark.parametrize(
    "url",
    [
        "https://www.twitch.tv/videos/123456",
        "https://clips.twitch.tv/SomeClipSlug",
        "https://m.twitch.tv/videos/123456",
    ],
)
def test_twitch_requires_campaign_permission(url: str) -> None:
    with pytest.raises(ComplianceError, match="allow_stream_download"):
        assert_registered_source(url, campaign(allow_stream_download=False), [url])
    assert assert_registered_source(url, campaign(allow_stream_download=True), [url]) is SourceHost.TWITCH


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://drive.google.com/file/d/abc/view", SourceHost.GDRIVE),
        ("https://drive.google.com/drive/folders/abc", SourceHost.GDRIVE),
        ("https://www.dropbox.com/scl/fi/abc/ep1.mp4?rlkey=x&dl=0", SourceHost.DROPBOX),
        ("https://dl.dropboxusercontent.com/s/abc/ep1.mp4", SourceHost.DROPBOX),
        ("https://cdn.example.com/a/b/EP1.MOV", SourceHost.DIRECT),
        ("file://inbox/dylan-page/ep1.mp4", SourceHost.INBOX),
        ("https://cdn.example.com/watch?v=1", SourceHost.OTHER),  # not a media file
        ("https://www.tiktok.com/@x/video/1.mp4", SourceHost.OTHER),  # platform, even with an extension
        ("https://contentrewards.com/files/x.mp4", SourceHost.OTHER),  # never scrape the marketplace
        ("ftp://cdn.example.com/a.mp4", SourceHost.OTHER),
        ("file:///etc/passwd", SourceHost.OTHER),
        ("not a url", SourceHost.OTHER),
    ],
)
def test_host_classification(url: str, host: SourceHost) -> None:
    assert classify_source_host(url) is host


def test_other_hosts_refused_even_if_registered() -> None:
    url = "https://www.instagram.com/reel/abc/"
    with pytest.raises(ComplianceError, match="not allowed"):
        assert_registered_source(url, campaign(), [url])


def test_downloader_refuses_youtube_source(factory: Factory, session: Session, settings: Settings) -> None:
    from clipforge.ingest.download import download_source  # type: ignore[attr-defined]

    c = factory.campaign(allow_stream_download=True)
    src = factory.source(c, url="https://www.youtube.com/watch?v=abc")
    with pytest.raises(ComplianceError):
        download_source(session, settings, src)


@pytest.mark.parametrize(
    ("url", "refused"),
    [
        ("https://www.tiktok.com/@x/video/1.mp4", True),
        ("https://cdn.instagram.com/v.mp4", True),
        ("https://x.com/a/status/1", True),
        ("https://twitter.com/a.mp4", True),
        ("https://m.facebook.com/v.mp4", True),
        ("https://fb.watch/abc", True),
        ("https://app.contentrewards.com/campaign/1", True),
        ("https://vyro.app/c/1", True),
        ("https://TikTok.com./v.mp4", True),
        ("not a url", True),
        ("https://cdn.example.com/vod.mp4", False),
        ("https://notiktok.com/v.mp4", False),
        ("https://dl.dropboxusercontent.com/s/a/v.mp4", False),
    ],
)
def test_is_refused_platform_url(url: str, refused: bool) -> None:
    """CR-A6: the public check the downloader uses for direct links and redirect targets."""
    from clipforge.compliance import is_refused_platform_url
    from clipforge.ingest.download import is_refused_platform_host

    assert is_refused_platform_url(url) is refused
    assert is_refused_platform_host(url) is refused


def test_other_channel_refused_before_any_media(tmp_path: Path) -> None:
    """A campaign that lists `stream_channels` never downloads a Twitch video from another channel: the metadata is
    read first (no media), and nothing is written."""
    from clipforge.ingest import twitch
    from tests.ingest.test_twitch_limits import MetaYDL

    MetaYDL.instances.clear()
    MetaYDL.meta = {"uploader_id": "someoneelse", "is_live": False}
    with pytest.raises(ComplianceError, match="someoneelse"):
        twitch.download(
            "https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=MetaYDL, channels=["fakestreamer"]
        )
    assert MetaYDL.instances[0].calls == [False]  # metadata only
    assert list(tmp_path.iterdir()) == []
