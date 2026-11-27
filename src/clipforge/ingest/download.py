"""Download registered sources (ARCHITECTURE §4.1, PRD F2, COMPLIANCE #1).

`download_source` is the only way footage enters clipforge. It always starts with
`compliance.assert_registered_source` (the URL must be a `sources` row of an active campaign; YouTube, Kick and other
hosts are never downloaded; Twitch only with `allow_stream_download`), then picks a handler by host:

- direct HTTP(S) media links: `httpx` streaming download, retried (tenacity, exponential, max 3 attempts).
- Dropbox file links: the same, with `dl=1` (https://help.dropbox.com/share/force-download).
- Google Drive file links: `gdown.download` (it uses `requests`, so tests replace `GDriveClient`).
- Twitch VODs/clips: `yt-dlp`, restricted to the Twitch extractors (`ingest.twitch`).
- `file://inbox/<slug>/...`: a file the operator dropped in `inbox/`, moved into the media dir (`ingest.inbox`).

After download: SHA-256, ffprobe duration, and status DOWNLOADED. Silent footage (no audio stream) is marked FAILED
`no_audio` (PRD F17: unsupported). A file identical to another source of the same campaign is FAILED `duplicate_of:N`.

Error contract for callers (pipeline, Phase 2):
- `ComplianceError`: the URL must never be downloaded. The source is marked FAILED `host_refused` (unless the
  campaign is merely inactive) and an operator alert is written; both are flushed, so **commit the session** after
  catching it.
- `DownloadError`: transient or content problem. The source stays REGISTERED (retried next night) and an alert is
  written; commit after catching it as well.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.wait import wait_base

from clipforge import compliance
from clipforge.compliance import ComplianceError
from clipforge.config import Settings
from clipforge.db import CampaignRow, SourceRow, utcnow
from clipforge.ingest import alerts, disk, media
from clipforge.ingest.models import campaign_from_row, set_source_status
from clipforge.schemas import CampaignStatus, SourceHost, SourceStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_WAIT: wait_base = wait_exponential(multiplier=2, min=2, max=60)
HTTP_TIMEOUT = httpx.Timeout(connect=20.0, read=120.0, write=60.0, pool=20.0)
MAX_DOWNLOAD_BYTES = 60 * 1024**3  # 60 GiB: well above a 10-hour 1080p VOD
STREAM_CHUNK = 1 << 20
USER_AGENT = "clipforge/0.1 (campaign footage downloader)"
DEFAULT_EXT = ".mp4"

# Redirects to these hosts abort a "direct" download: they may only be reached through their own (gated) handler.
_REDIRECT_REFUSED: frozenset[SourceHost] = frozenset({SourceHost.YOUTUBE, SourceHost.KICK, SourceHost.TWITCH})


class DownloadError(RuntimeError):
    """A download failed for a reason that is not a compliance refusal."""


class DiskSpaceError(DownloadError):
    """Not enough free disk for the download (ingest/disk.py); tried again next night."""


class _RetryableStatus(DownloadError):
    pass


# --------------------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------------------


def dropbox_direct_url(url: str) -> str:
    """Force a direct download of a Dropbox shared *file* link: set `dl=1`, keep every other parameter."""
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.endswith("dropboxusercontent.com"):
        return url  # already a direct content host
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("dl", "raw")]
    query.append(("dl", "1"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def media_suffix(name: str) -> str:
    """The file's media extension (lowercase) if it is one we accept, else `.mp4`."""
    suffix = Path(urlsplit(name).path if "://" in name else name).suffix.lower()
    return suffix if suffix in compliance.MEDIA_EXTENSIONS else DEFAULT_EXT


def source_dir(settings: Settings, campaign_id: int) -> Path:
    return settings.media_dir / "sources" / str(campaign_id)


# --------------------------------------------------------------------------------------------------
# HTTP handler (direct + Dropbox)
# --------------------------------------------------------------------------------------------------


def is_refused_platform_host(url: str) -> bool:
    """True if `url`'s host is a platform/marketplace compliance never downloads from (TikTok, Instagram, X,
    Facebook, Content Rewards, Vyro, ...). Delegates to `compliance.is_refused_platform_url` (CR-A6)."""
    return compliance.is_refused_platform_url(url)


def _refuse_bad_redirect(response: httpx.Response) -> None:
    if not response.has_redirect_location:
        return
    target = str(response.request.url.join(response.headers["location"]))
    host = compliance.classify_source_host(target)
    if host in _REDIRECT_REFUSED:
        raise ComplianceError(1, f"download redirected to a {host.value} URL, which this handler never fetches")
    if is_refused_platform_host(target):
        raise ComplianceError(1, f"download redirected to {urlsplit(target).hostname}, a platform never downloaded")


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, _RetryableStatus | httpx.TransportError)


def http_download(url: str, dest: Path) -> str:
    """Stream `url` to `dest` (via `dest.part`), return its SHA-256. Retries transport errors, 429 and 5xx."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    retrying = Retrying(
        stop=stop_after_attempt(MAX_ATTEMPTS),
        wait=RETRY_WAIT,
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    try:
        for attempt in retrying:
            with attempt:
                digest = _http_download_once(url, part)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"HTTP error downloading source: {type(exc).__name__}: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, dest)
    return digest


def _http_download_once(url: str, part: Path) -> str:
    with (
        httpx.Client(
            timeout=HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            event_hooks={"response": [_refuse_bad_redirect]},
        ) as client,
        client.stream("GET", url) as resp,
    ):
        if resp.status_code == 429 or resp.status_code >= 500:
            raise _RetryableStatus(f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise DownloadError(f"HTTP {resp.status_code} (is the link shared publicly?)")
        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype in ("text/html", "application/xhtml+xml"):
            raise DownloadError("the link returned a web page, not a media file (not shared publicly, or a folder?)")
        h = hashlib.sha256()
        size = 0
        with open(part, "wb") as fh:
            for chunk in resp.iter_bytes(STREAM_CHUNK):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise DownloadError(f"file is larger than {MAX_DOWNLOAD_BYTES // 1024**3} GiB")
                h.update(chunk)
                fh.write(chunk)
        if size == 0:
            raise DownloadError("empty response body")
        return h.hexdigest()


# --------------------------------------------------------------------------------------------------
# Google Drive
# --------------------------------------------------------------------------------------------------


class GDriveClient:
    """Thin adapter over gdown (https://github.com/wkentaro/gdown; signatures checked against gdown 6.5.2).

    `gdown.download(url=..., output=<dir>/)` parses `/file/d/<id>/view` links itself and keeps Drive's file name when
    `output` ends with a path separator. gdown uses `requests`, so tests substitute this class instead of respx.
    """

    def download(self, url: str, out_dir: Path) -> Path:
        import gdown

        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = gdown.download(url=url, output=str(out_dir) + os.sep, quiet=True, retries=MAX_ATTEMPTS - 1)
        except Exception as exc:  # gdown raises its own FileURLRetrievalError and requests errors
            raise DownloadError(f"Google Drive download failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(result, str):
            raise DownloadError(
                "Google Drive download failed (no file; is the link shared with 'Anyone with the link'?)"
            )
        return Path(result)


# --------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------

Fetcher = Callable[[SourceRow, CampaignRow, Path], Path]


def download_source(
    session: Session,
    settings: Settings,
    source: SourceRow,
    *,
    gdrive: GDriveClient | None = None,
    ytdl_factory: Callable[[dict[str, object]], object] | None = None,
    runner: media.Runner = media.run,
) -> SourceRow:
    """Download one registered source; see the module docstring for the error contract."""
    campaign = session.get(CampaignRow, source.campaign_id)
    if campaign is None:  # pragma: no cover - FK guarantees this
        raise DownloadError(f"source {source.id} has no campaign")
    registered = session.scalars(select(SourceRow.url).where(SourceRow.campaign_id == campaign.id)).all()

    # COMPLIANCE #1: always first, whatever the source's status.
    try:
        host = compliance.assert_registered_source(source.url, campaign_from_row(campaign), registered)
    except ComplianceError as exc:
        if campaign.status is CampaignStatus.ACTIVE:
            set_source_status(session, source, SourceStatus.FAILED, fail_reason="host_refused", detail=str(exc))
            alerts.alert_operator(
                session, alerts.HOST_REFUSED, source.id, f"download refused: {alerts.redact_urls(str(exc))}"
            )
        else:
            alerts.alert_operator(
                session, alerts.CAMPAIGN_INACTIVE, source.id, f"download refused: {alerts.redact_urls(str(exc))}"
            )
        raise

    if source.status is not SourceStatus.REGISTERED:
        if source.status is SourceStatus.FAILED:
            raise DownloadError(f"source {source.id} failed earlier ({source.fail_reason}); not downloading again")
        return source  # already downloaded (or further along)

    out_dir = source_dir(settings, campaign.id)
    try:
        path, digest = _fetch(host, source, campaign, out_dir, settings, gdrive=gdrive, ytdl_factory=ytdl_factory)
    except ComplianceError as exc:  # e.g. a redirect to YouTube
        set_source_status(session, source, SourceStatus.FAILED, fail_reason="host_refused", detail=str(exc))
        alerts.alert_operator(
            session, alerts.HOST_REFUSED, source.id, f"download refused: {alerts.redact_urls(str(exc))}"
        )
        raise
    except (DownloadError, OSError) as exc:
        alerts.alert_operator(
            session, alerts.DOWNLOAD_FAILED, source.id, f"download failed (will retry): {alerts.redact_urls(str(exc))}"
        )
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(str(exc)) from exc

    return _finish(session, source, path, digest, runner)


def _fetch(
    host: SourceHost,
    source: SourceRow,
    campaign: CampaignRow,
    out_dir: Path,
    settings: Settings,
    *,
    gdrive: GDriveClient | None,
    ytdl_factory: Callable[[dict[str, object]], object] | None,
) -> tuple[Path, str]:
    """Download with the handler for `host`; return (final path, sha256)."""
    stem = str(source.id)
    if host in (SourceHost.DIRECT, SourceHost.DROPBOX, SourceHost.GDRIVE):  # Twitch checks the VOD's own size below
        disk.check_free(out_dir, None, settings.app.min_free_gb)
    if host is SourceHost.DIRECT:
        dest = out_dir / f"{stem}{media_suffix(source.url)}"
        return dest, http_download(source.url, dest)
    if host is SourceHost.DROPBOX:
        dest = out_dir / f"{stem}{media_suffix(source.url)}"
        return dest, http_download(dropbox_direct_url(source.url), dest)
    if host is SourceHost.GDRIVE:
        tmp_dir = out_dir / f".gdrive-{stem}"
        got = (gdrive or GDriveClient()).download(source.url, tmp_dir)
        dest = out_dir / f"{stem}{media_suffix(got.name)}"
        os.replace(got, dest)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return dest, media.sha256_file(dest)
    if host is SourceHost.TWITCH:
        from clipforge.ingest import twitch

        # assert_registered_source already required allow_stream_download; check again at the point of action.
        if not campaign.allow_stream_download:
            raise ComplianceError(1, f"campaign {campaign.id} does not allow stream downloads")

        def disk_check(meta: dict[str, Any]) -> None:
            disk.check_free(out_dir, disk.estimate_bytes(meta), settings.app.min_free_gb)

        got = twitch.download(
            source.url,
            out_dir,
            stem,
            ytdl_factory=ytdl_factory,
            channels=campaign.stream_channels,
            max_minutes=campaign.max_source_minutes,
            disk_check=disk_check,
        )
        return got, media.sha256_file(got)
    if host is SourceHost.INBOX:
        from clipforge.ingest import inbox

        dest_stem = out_dir / stem
        got = inbox.take(settings, campaign, source.url, dest_stem)
        return got, media.sha256_file(got)
    raise ComplianceError(1, f"no download handler for host {host.value}")  # pragma: no cover


def _finish(session: Session, source: SourceRow, path: Path, digest: str, runner: media.Runner) -> SourceRow:
    source.local_path = str(path)
    source.sha256 = digest
    source.downloaded_at = utcnow()
    try:
        info = media.probe(path, runner)
    except media.MediaToolError as exc:
        set_source_status(session, source, SourceStatus.FAILED, fail_reason="unreadable", detail=str(exc))
        alerts.alert_operator(
            session, alerts.DOWNLOAD_FAILED, source.id, f"downloaded file is not readable media: {exc}"
        )
        return source
    source.duration_s = info.duration_s

    dup = session.scalar(
        select(SourceRow.id).where(
            SourceRow.campaign_id == source.campaign_id,
            SourceRow.sha256 == digest,
            SourceRow.id != source.id,
        )
    )
    if dup is not None:
        set_source_status(session, source, SourceStatus.FAILED, fail_reason=f"duplicate_of:{dup}")
        alerts.alert_operator(session, alerts.DUPLICATE_SOURCE, source.id, f"same file as source {dup}; skipped")
        return source
    if not info.has_audio:
        # PRD F17: silent footage is unsupported (ranking needs speech); flag it instead of producing weak clips.
        set_source_status(session, source, SourceStatus.FAILED, fail_reason="no_audio")
        alerts.alert_operator(session, alerts.NO_SPEECH, source.id, "footage has no audio track; v1 cannot clip it")
        return source

    set_source_status(
        session, source, SourceStatus.DOWNLOADED, detail=f"sha256={digest[:12]} duration={info.duration_s:.0f}s"
    )
    log.info("source %s downloaded (%.0f s)", source.id, info.duration_s)
    return source
