"""A2c: Twitch via yt-dlp only with campaign permission; inbox/ drop folder."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.compliance import ComplianceError
from clipforge.config import Settings
from clipforge.db import EventRow, SourceRow
from clipforge.ingest import inbox, media, twitch
from clipforge.ingest.download import DownloadError, download_source
from clipforge.schemas import SourceStatus
from tests.support.factories import Factory

BODY = b"twitch-video-bytes" * 100


def probe_runner(args: Sequence[str]) -> str:
    return json.dumps({"format": {"duration": "1800"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]})


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL: records options, writes the file the outtmpl names."""

    instances: list[FakeYDL] = []

    def __init__(self, opts: dict[str, Any], *, extractor: str = "TwitchVod", write: bool = True) -> None:
        self.opts = opts
        self.extractor = extractor
        self.write = write
        self.urls: list[str] = []
        FakeYDL.instances.append(self)

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
        self.urls.append(url)
        path = Path(self.opts["outtmpl"]["default"].replace("%(ext)s", "mp4"))
        if self.write:
            path.write_bytes(BODY)
        return {"extractor_key": self.extractor, "requested_downloads": [{"filepath": str(path)}]}


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeYDL.instances.clear()


# --- Twitch -------------------------------------------------------------------------------------------


def test_twitch_download_with_permission(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(allow_stream_download=True)
    src = factory.source(c, url="https://www.twitch.tv/videos/123456")
    download_source(session, settings, src, ytdl_factory=FakeYDL, runner=probe_runner)
    assert src.status is SourceStatus.DOWNLOADED
    assert src.sha256 == hashlib.sha256(BODY).hexdigest()
    (ydl,) = FakeYDL.instances
    # metadata first (the disk-space check reads the VOD's size), then the download
    assert ydl.urls == ["https://www.twitch.tv/videos/123456", "https://www.twitch.tv/videos/123456"]
    assert ydl.opts["allowed_extractors"] == ["twitch:vod", "twitch:clips"]
    assert ydl.opts["noplaylist"] is True
    assert Path(src.local_path or "").name == f"{src.id}.mp4"


@pytest.mark.parametrize(
    "url",
    ["https://www.youtube.com/watch?v=abc", "https://kick.com/x/videos/1", "https://www.twitch.tv/videos/1"],
)
def test_ytdlp_never_constructed_without_permission(
    session: Session, settings: Settings, factory: Factory, url: str
) -> None:
    c = factory.campaign(allow_stream_download="twitch" not in url)  # YouTube/Kick refused even with permission
    src = factory.source(c, url=url)
    with pytest.raises(ComplianceError):
        download_source(session, settings, src, ytdl_factory=FakeYDL, runner=probe_runner)
    assert FakeYDL.instances == []


def test_twitch_handler_refuses_non_twitch_url(tmp_path: Path) -> None:
    with pytest.raises(ComplianceError, match="twitch.tv"):
        twitch.download("https://www.youtube.com/watch?v=abc", tmp_path, "1", ytdl_factory=FakeYDL)
    assert FakeYDL.instances == []


def test_non_twitch_extractor_result_discarded(tmp_path: Path) -> None:
    def factory(opts: dict[str, Any]) -> FakeYDL:
        return FakeYDL(opts, extractor="Generic")

    with pytest.raises(ComplianceError, match="only Twitch"):
        twitch.download("https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=factory)
    assert list(tmp_path.iterdir()) == []


def test_ytdlp_error_becomes_download_error(tmp_path: Path) -> None:
    class Boom(FakeYDL):
        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            raise RuntimeError("Video unavailable (VOD expired)")

    with pytest.raises(DownloadError, match="expired"):
        twitch.download("https://www.twitch.tv/videos/1", tmp_path, "1", ytdl_factory=Boom)


def test_no_file_written(tmp_path: Path) -> None:
    def factory(opts: dict[str, Any]) -> FakeYDL:
        return FakeYDL(opts, write=False)

    with pytest.raises(DownloadError, match="no file"):
        twitch.download("https://clips.twitch.tv/Slug", tmp_path, "1", ytdl_factory=factory)


def test_ytdl_options_pure(tmp_path: Path) -> None:
    opts = twitch.ytdl_options(tmp_path, "7")
    assert opts["outtmpl"] == {"default": str(tmp_path / "7.%(ext)s")}
    assert "cookiefile" not in opts  # never logs in to anything


def test_allowed_extractors_exist_in_ytdlp() -> None:
    from yt_dlp.extractor import gen_extractor_classes

    names = {ie.IE_NAME for ie in gen_extractor_classes()}
    assert set(twitch.ALLOWED_EXTRACTORS) <= names


# --- inbox --------------------------------------------------------------------------------------------


def _drop(settings: Settings, slug: str, rel: str, data: bytes = BODY, age_s: float = 600) -> Path:
    p = settings.inbox_dir / slug / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    old = time.time() - age_s
    os.utime(p, (old, old))
    return p


def _alerts(session: Session) -> list[EventRow]:
    return list(session.scalars(select(EventRow).where(EventRow.entity == "alert")))


def test_scan_registers_media_files(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(name="Dylan Page")
    _drop(settings, c.slug, "ep 1.mp4")
    _drop(settings, c.slug, "day2/ep2.MOV")
    _drop(settings, c.slug, "notes.txt")
    _drop(settings, c.slug, ".hidden.mp4")
    _drop(settings, c.slug, "copying.mp4.part")
    _drop(settings, c.slug, "fresh.mp4", age_s=5)  # still being copied
    rows = inbox.scan_inbox(session, settings)
    assert sorted(r.url for r in rows) == [
        f"file://inbox/{c.slug}/day2/ep2.MOV",
        f"file://inbox/{c.slug}/ep%201.mp4",
    ]
    # Idempotent.
    assert inbox.scan_inbox(session, settings) == []
    assert len(session.scalars(select(SourceRow)).all()) == 2


def test_scan_unknown_and_inactive_campaigns_alert(session: Session, settings: Settings, factory: Factory) -> None:
    ended = factory.campaign(name="Old One", status="ended")
    _drop(settings, "no-such-campaign", "a.mp4")
    _drop(settings, ended.slug, "b.mp4")
    assert inbox.scan_inbox(session, settings) == []
    assert sorted(a.to_state or "" for a in _alerts(session)) == ["campaign_inactive", "inbox_unknown_campaign"]


def test_scan_without_inbox_dir(session: Session, settings: Settings) -> None:
    assert inbox.scan_inbox(session, settings) == []


def test_inbox_download_moves_and_hashes(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(name="Dylan Page")
    dropped = _drop(settings, c.slug, "ep 1.MP4")
    (src,) = inbox.scan_inbox(session, settings)
    download_source(session, settings, src, runner=probe_runner)
    assert src.status is SourceStatus.DOWNLOADED
    assert not dropped.exists()
    path = Path(src.local_path or "")
    assert path == settings.media_dir / "sources" / str(c.id) / f"{src.id}.mp4"
    assert media.sha256_file(path) == hashlib.sha256(BODY).hexdigest() == src.sha256


def test_inbox_file_gone(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(name="Dylan Page")
    dropped = _drop(settings, c.slug, "ep1.mp4")
    (src,) = inbox.scan_inbox(session, settings)
    dropped.unlink()
    with pytest.raises(DownloadError, match="gone"):
        download_source(session, settings, src, runner=probe_runner)


@pytest.mark.parametrize("rel", ["../other/ep1.mp4", "../../etc/passwd.mp4", "a/../../x.mp4"])
def test_inbox_path_traversal_refused(session: Session, settings: Settings, factory: Factory, rel: str) -> None:
    c = factory.campaign(name="Dylan Page")
    _drop(settings, "other", "ep1.mp4")
    src = factory.source(c, url=f"file://inbox/{c.slug}/{rel}")
    with pytest.raises(ComplianceError, match="escapes|another campaign"):
        download_source(session, settings, src, runner=probe_runner)
    assert (settings.inbox_dir / "other" / "ep1.mp4").exists()


def test_inbox_other_campaigns_folder_refused(session: Session, settings: Settings, factory: Factory) -> None:
    mine = factory.campaign(name="Mine")
    theirs = factory.campaign(name="Theirs")
    _drop(settings, theirs.slug, "ep1.mp4")
    src = factory.source(mine, url=f"file://inbox/{theirs.slug}/ep1.mp4")
    with pytest.raises(ComplianceError, match="another campaign"):
        download_source(session, settings, src, runner=probe_runner)


def test_inbox_symlink_refused(session: Session, settings: Settings, factory: Factory, tmp_path: Path) -> None:
    c = factory.campaign(name="Dylan Page")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(BODY)
    link = settings.inbox_dir / c.slug / "link.mp4"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert inbox.scan_inbox(session, settings) == []  # scan skips symlinks
    src = factory.source(c, url=f"file://inbox/{c.slug}/link.mp4")
    with pytest.raises(ComplianceError, match="symlink"):
        download_source(session, settings, src, runner=probe_runner)
    assert outside.exists()


def test_parse_inbox_url() -> None:
    assert inbox.parse_inbox_url("file://inbox/dylan/ep%201.mp4") == ("dylan", "ep 1.mp4")
    with pytest.raises(ComplianceError):
        inbox.parse_inbox_url("file://inbox/dylan")
    with pytest.raises(ComplianceError):
        inbox.parse_inbox_url("file:///etc/passwd")


def test_symlinked_campaign_folder_refused(session: Session, settings: Settings, factory: Factory) -> None:
    mine = factory.campaign(name="Mine")
    theirs = factory.campaign(name="Theirs")
    _drop(settings, theirs.slug, "ep1.mp4")
    (settings.inbox_dir / mine.slug).symlink_to(settings.inbox_dir / theirs.slug)
    rows = inbox.scan_inbox(session, settings)
    assert [r.campaign_id for r in rows] == [theirs.id]  # nothing registered for "mine" through the link
    src = factory.source(mine, url=f"file://inbox/{mine.slug}/ep1.mp4")
    with pytest.raises(ComplianceError, match="symlink"):
        download_source(session, settings, src, runner=probe_runner)
    assert (settings.inbox_dir / theirs.slug / "ep1.mp4").exists()
