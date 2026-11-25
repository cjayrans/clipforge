"""Campaign stream limits for Twitch (generic, fake campaign): only the listed channels, only the first N minutes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from clipforge.compliance import ComplianceError
from clipforge.config import Settings
from clipforge.ingest import twitch
from clipforge.ingest.download import DownloadError, download_source
from clipforge.schemas import SourceStatus
from tests.ingest.test_twitch_inbox import BODY, probe_runner
from tests.support.factories import Factory


class MetaYDL:
    """Fake yt_dlp.YoutubeDL: metadata calls (download=False) write nothing; download calls write the file."""

    instances: list[MetaYDL] = []
    meta: dict[str, Any] = {}

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.calls: list[bool] = []
        MetaYDL.instances.append(self)

    def __enter__(self) -> MetaYDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        self.calls.append(download)
        info = {"extractor_key": "TwitchVod", **MetaYDL.meta}
        if download:
            path = Path(self.opts["outtmpl"]["default"].replace("%(ext)s", "mp4"))
            path.write_bytes(BODY)
            info["requested_downloads"] = [{"filepath": str(path)}]
        return info


@pytest.fixture(autouse=True)
def _reset() -> None:
    MetaYDL.instances.clear()
    MetaYDL.meta = {"uploader_id": "fakestreamer", "uploader": "FakeStreamer", "is_live": False}


def test_first_minutes_and_channel_checked_before_download(
    session: Session, settings: Settings, factory: Factory
) -> None:
    c = factory.campaign(allow_stream_download=True, stream_channels=["fakestreamer"], max_source_minutes=60)
    src = factory.source(c, url="https://www.twitch.tv/videos/111")
    download_source(session, settings, src, ytdl_factory=MetaYDL, runner=probe_runner)
    assert src.status is SourceStatus.DOWNLOADED
    (ydl,) = MetaYDL.instances
    assert ydl.calls == [False, True]  # metadata first, then the download
    ranges = ydl.opts["download_ranges"]({}, None)
    assert ranges == [{"start_time": 0.0, "end_time": 3600.0}]


def test_unknown_channel_refused(tmp_path: Path) -> None:
    MetaYDL.meta = {"is_live": False}
    with pytest.raises(ComplianceError, match="unknown"):
        twitch.download(
            "https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=MetaYDL, channels=["fakestreamer"]
        )


def test_still_live_vod_refused(tmp_path: Path) -> None:
    MetaYDL.meta = {"uploader_id": "fakestreamer", "is_live": True}
    with pytest.raises(DownloadError, match="still live"):
        twitch.download(
            "https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=MetaYDL, channels=["fakestreamer"]
        )
    assert MetaYDL.instances[0].calls == [False]


def test_clip_channel_comes_from_channel_field(tmp_path: Path) -> None:
    assert twitch.channel_of({"extractor_key": "TwitchClips", "channel": "FakeStreamer", "uploader_id": "999"}) == (
        "fakestreamer"
    )
    assert twitch.channel_of({"extractor_key": "TwitchVod", "uploader_id": "FakeStreamer"}) == "fakestreamer"


def test_no_limits_means_one_call_and_whole_video(tmp_path: Path) -> None:
    twitch.download("https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=MetaYDL)
    (ydl,) = MetaYDL.instances
    assert ydl.calls == [True]
    assert "download_ranges" not in ydl.opts


def test_download_ranges_option_is_what_ytdlp_expects() -> None:
    """yt-dlp's documented `download_ranges` callback shape: (info_dict, ydl) -> iterable of section dicts."""
    import inspect

    import yt_dlp  # type: ignore[import-untyped]

    assert "download_ranges" in inspect.getdoc(yt_dlp.YoutubeDL)  # type: ignore[operator]
    opts = twitch.ytdl_options(Path("/tmp"), "1", max_seconds=90)
    assert opts["download_ranges"]({"id": "x"}, object()) == [{"start_time": 0.0, "end_time": 90.0}]
