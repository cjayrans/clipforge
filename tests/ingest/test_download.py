"""A2: download of registered sources (direct, Dropbox file, Drive file) with COMPLIANCE #1 first."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import wait_none

from clipforge.compliance import ComplianceError
from clipforge.config import Settings
from clipforge.db import EventRow
from clipforge.ingest import download, media
from clipforge.ingest.download import DownloadError, download_source
from clipforge.schemas import SourceStatus
from tests.support.factories import Factory

FIXTURE_VIDEO = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_90s.mp4"
BODY = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000


def probe_runner(duration: float = 90.0, kinds: Sequence[str] = ("video", "audio")) -> media.Runner:
    def run(args: Sequence[str]) -> str:
        assert args[0] == "ffprobe"
        return json.dumps({"format": {"duration": str(duration)}, "streams": [{"codec_type": k} for k in kinds]})

    return run


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "RETRY_WAIT", wait_none())


def _alert_kinds(session: Session) -> list[str | None]:
    return [e.to_state for e in session.scalars(select(EventRow).where(EventRow.entity == "alert"))]


# --- compliance first ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc",
        "https://kick.com/someone/videos/1",
        "https://www.instagram.com/reel/abc/",
    ],
)
@respx.mock(assert_all_called=False)
def test_refused_hosts_never_make_a_request(
    session: Session, settings: Settings, factory: Factory, url: str, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.route()  # any request at all
    c = factory.campaign(allow_stream_download=True)
    src = factory.source(c, url=url)  # a row can exist (e.g. inserted by hand); the downloader still refuses
    with pytest.raises(ComplianceError):
        download_source(session, settings, src, runner=probe_runner())
    assert not route.called
    assert src.status is SourceStatus.FAILED
    assert src.fail_reason == "host_refused"
    assert _alert_kinds(session) == ["host_refused"]


@respx.mock(assert_all_called=False)
def test_twitch_without_permission_refused(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(allow_stream_download=False)
    src = factory.source(c, url="https://www.twitch.tv/videos/123")

    def factory_must_not_run(opts: dict[str, object]) -> object:
        raise AssertionError("yt-dlp must not be constructed")

    with pytest.raises(ComplianceError, match="allow_stream_download"):
        download_source(session, settings, src, ytdl_factory=factory_must_not_run)
    assert src.status is SourceStatus.FAILED


@pytest.mark.parametrize("status", ["drained", "ended"])
def test_inactive_campaign_refused_but_source_not_failed(
    session: Session, settings: Settings, factory: Factory, status: str
) -> None:
    c = factory.campaign(status=status)
    src = factory.source(c)
    with pytest.raises(ComplianceError, match="not active"):
        download_source(session, settings, src, runner=probe_runner())
    assert src.status is SourceStatus.REGISTERED
    assert _alert_kinds(session) == ["campaign_inactive"]


# --- direct HTTP --------------------------------------------------------------------------------------


@respx.mock
def test_direct_download_hash_duration_and_event(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign()
    src = factory.source(c, url="https://cdn.example.com/campaign/EP1.MOV")
    respx.get("https://cdn.example.com/campaign/EP1.MOV").respond(
        200, content=BODY, headers={"content-type": "video/quicktime"}
    )
    out = download_source(session, settings, src, runner=probe_runner(3600.5))
    assert out.status is SourceStatus.DOWNLOADED
    assert out.sha256 == hashlib.sha256(BODY).hexdigest()
    assert out.duration_s == 3600.5
    assert out.downloaded_at is not None
    path = Path(out.local_path or "")
    assert path == settings.media_dir / "sources" / str(c.id) / f"{src.id}.mov"
    assert path.read_bytes() == BODY
    assert not list(path.parent.glob("*.part"))
    ev = session.scalars(select(EventRow).where(EventRow.entity == "source")).all()
    assert (ev[-1].from_state, ev[-1].to_state) == ("registered", "downloaded")
    # Idempotent: a second call makes no request and changes nothing.
    assert download_source(session, settings, out, runner=probe_runner()) is out


@respx.mock
def test_retries_5xx_then_succeeds(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    route = respx.get(src.url).mock(
        side_effect=[httpx.Response(503), httpx.ConnectError("boom"), httpx.Response(200, content=BODY)]
    )
    download_source(session, settings, src, runner=probe_runner())
    assert route.call_count == 3
    assert src.status is SourceStatus.DOWNLOADED


@respx.mock
def test_gives_up_after_three_attempts(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    route = respx.get(src.url).respond(429)
    with pytest.raises(DownloadError):
        download_source(session, settings, src, runner=probe_runner())
    assert route.call_count == 3
    assert src.status is SourceStatus.REGISTERED  # transient: retried next night
    assert _alert_kinds(session) == ["download_failed"]
    assert not list(download.source_dir(settings, src.campaign_id).glob("*"))


@respx.mock
def test_404_not_retried(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    route = respx.get(src.url).respond(404)
    with pytest.raises(DownloadError, match="404"):
        download_source(session, settings, src, runner=probe_runner())
    assert route.call_count == 1


@respx.mock
def test_html_page_refused(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    respx.get(src.url).respond(200, html="<html>sign in</html>")
    with pytest.raises(DownloadError, match="web page"):
        download_source(session, settings, src, runner=probe_runner())


@respx.mock
def test_redirect_to_youtube_refused(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source(url="https://cdn.example.com/ep1.mp4")
    respx.get(src.url).respond(302, headers={"location": "https://www.youtube.com/watch?v=abc"})
    yt = respx.get("https://www.youtube.com/watch?v=abc").respond(200, content=BODY)
    with pytest.raises(ComplianceError, match="redirected"):
        download_source(session, settings, src, runner=probe_runner())
    assert not yt.called
    assert src.status is SourceStatus.FAILED
    assert src.fail_reason == "host_refused"


@respx.mock
def test_redirect_to_cdn_followed(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source(url="https://cdn.example.com/ep1.mp4")
    respx.get(src.url).respond(302, headers={"location": "https://edge.example.net/abc?sig=1"})
    respx.get("https://edge.example.net/abc?sig=1").respond(200, content=BODY)
    download_source(session, settings, src, runner=probe_runner())
    assert src.status is SourceStatus.DOWNLOADED


@respx.mock
def test_size_cap(session: Session, settings: Settings, factory: Factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "MAX_DOWNLOAD_BYTES", 100)
    src = factory.source()
    respx.get(src.url).respond(200, content=BODY)
    with pytest.raises(DownloadError, match="larger"):
        download_source(session, settings, src, runner=probe_runner())


# --- post-download checks -----------------------------------------------------------------------------


@respx.mock
def test_silent_footage_flagged(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    respx.get(src.url).respond(200, content=BODY)
    download_source(session, settings, src, runner=probe_runner(kinds=("video",)))
    assert src.status is SourceStatus.FAILED
    assert src.fail_reason == "no_audio"
    assert _alert_kinds(session) == ["no_speech"]


@respx.mock
def test_unreadable_media_flagged(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    respx.get(src.url).respond(200, content=BODY)

    def broken(args: Sequence[str]) -> str:
        raise media.MediaToolError("ffprobe failed")

    download_source(session, settings, src, runner=broken)
    assert src.status is SourceStatus.FAILED
    assert src.fail_reason == "unreadable"


@respx.mock
def test_duplicate_file_flagged(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign()
    a = factory.source(c, url="https://cdn.example.com/a.mp4")
    b = factory.source(c, url="https://cdn.example.com/b.mp4")
    respx.get(a.url).respond(200, content=BODY)
    respx.get(b.url).respond(200, content=BODY)
    download_source(session, settings, a, runner=probe_runner())
    download_source(session, settings, b, runner=probe_runner())
    assert a.status is SourceStatus.DOWNLOADED
    assert b.status is SourceStatus.FAILED
    assert b.fail_reason == f"duplicate_of:{a.id}"


def test_failed_source_not_retried(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source(status=SourceStatus.FAILED, fail_reason="no_audio")
    with pytest.raises(DownloadError, match="failed earlier"):
        download_source(session, settings, src, runner=probe_runner())


# --- Dropbox and Drive --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://www.dropbox.com/scl/fi/abc/ep1.mp4?rlkey=xyz&dl=0",
            "https://www.dropbox.com/scl/fi/abc/ep1.mp4?rlkey=xyz&dl=1",
        ),
        ("https://www.dropbox.com/s/abc/ep1.mp4", "https://www.dropbox.com/s/abc/ep1.mp4?dl=1"),
        ("https://www.dropbox.com/s/abc/ep1.mp4?raw=1", "https://www.dropbox.com/s/abc/ep1.mp4?dl=1"),
        ("https://dl.dropboxusercontent.com/s/abc/ep1.mp4", "https://dl.dropboxusercontent.com/s/abc/ep1.mp4"),
    ],
)
def test_dropbox_direct_url(url: str, expected: str) -> None:
    assert download.dropbox_direct_url(url) == expected


@respx.mock
def test_dropbox_file_download(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source(url="https://www.dropbox.com/scl/fi/abc/ep1.mp4?rlkey=xyz&dl=0")
    route = respx.get("https://www.dropbox.com/scl/fi/abc/ep1.mp4?rlkey=xyz&dl=1").respond(200, content=BODY)
    download_source(session, settings, src, runner=probe_runner())
    assert route.called
    assert src.status is SourceStatus.DOWNLOADED
    assert Path(src.local_path or "").suffix == ".mp4"


class FakeGDrive(download.GDriveClient):
    def __init__(self, body: bytes = BODY, name: str = "Episode 1.MKV", fail: bool = False) -> None:
        self.body, self.name, self.fail = body, name, fail
        self.calls: list[str] = []

    def download(self, url: str, out_dir: Path) -> Path:
        self.calls.append(url)
        if self.fail:
            raise DownloadError("Google Drive download failed (quota)")
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / self.name
        p.write_bytes(self.body)
        return p


def test_gdrive_download(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source(url="https://drive.google.com/file/d/FILEID/view")
    gd = FakeGDrive()
    download_source(session, settings, src, gdrive=gd, runner=probe_runner())
    assert gd.calls == ["https://drive.google.com/file/d/FILEID/view"]
    assert src.status is SourceStatus.DOWNLOADED
    assert src.sha256 == hashlib.sha256(BODY).hexdigest()
    path = Path(src.local_path or "")
    assert path.name == f"{src.id}.mkv"
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # temp dir cleaned


def test_gdrive_failure_alerts(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source(url="https://drive.google.com/file/d/FILEID/view")
    with pytest.raises(DownloadError, match="quota"):
        download_source(session, settings, src, gdrive=FakeGDrive(fail=True), runner=probe_runner())
    assert src.status is SourceStatus.REGISTERED
    assert _alert_kinds(session) == ["download_failed"]


def test_media_suffix() -> None:
    assert download.media_suffix("https://x/a/EP.MOV?x=1") == ".mov"
    assert download.media_suffix("https://x/a/ep") == ".mp4"
    assert download.media_suffix("clip.webm") == ".webm"
    assert download.media_suffix("notes.txt") == ".mp4"


# --- media utils --------------------------------------------------------------------------------------


def test_parse_ffprobe() -> None:
    p = media.parse_ffprobe(json.dumps({"format": {"duration": "12.5"}, "streams": [{"codec_type": "audio"}]}))
    assert p == media.Probe(duration_s=12.5, has_audio=True, has_video=False)
    with pytest.raises(media.MediaToolError):
        media.parse_ffprobe("{}")
    with pytest.raises(media.MediaToolError):
        media.parse_ffprobe("not json")


def test_run_missing_tool() -> None:
    with pytest.raises(media.MediaToolError, match="not installed"):
        media.run(["clipforge-no-such-tool-xyz"])


@pytest.mark.needs_ffmpeg
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed")
def test_real_ffprobe_on_fixture() -> None:
    p = media.probe(FIXTURE_VIDEO)
    assert p.duration_s == pytest.approx(90.0, abs=0.5)
    assert p.has_audio
    assert p.has_video
