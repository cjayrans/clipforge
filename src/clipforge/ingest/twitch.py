"""Twitch VOD / clip download via yt-dlp (PRD F2, COMPLIANCE #1).

Only `ingest.download.download_source` calls this, after `compliance.assert_registered_source` has confirmed the URL
is registered on an active campaign with `allow_stream_download=true`.

yt-dlp facts (checked against the installed yt-dlp 2026.10.26 source, `YoutubeDL` docstring and
`_load_extractors`; README https://github.com/yt-dlp/yt-dlp):
- `allowed_extractors` is a list of regexes matched with `re.fullmatch` (case-insensitive) against extractor names, so
  `["twitch:vod", "twitch:clips"]` loads exactly those two extractors and nothing else (no generic extractor).
- The downloaded file is `info["requested_downloads"][0]["filepath"]`.
- Channel of a VOD: `uploader_id` is the channel login (`TwitchVodIE`, from GQL `owner.login`). For a clip
  (`TwitchClipsIE`) `uploader_id` is the clipper; the channel is `channel` (display name, compared lowercased).
  `is_live` is True while the VOD is still recording.
- `download_ranges`: a callback `(info_dict, ydl) -> Iterable[{"start_time", "end_time"}]`; only those sections are
  downloaded (yt-dlp hands them to ffmpeg; `force_keyframes_at_cuts` stays off, so the cut is at a keyframe).
Campaign options (generic, from `schemas.CampaignIn`): `stream_channels` limits which channels' VODs/clips may be
downloaded (checked on the metadata before any media is fetched, and again on the result), and
`max_source_minutes` downloads only the first N minutes.
Twitch VODs expire (up to 60 days) and clips are <= 60 s: register early, download once, keep the hash.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from clipforge import compliance
from clipforge.compliance import ComplianceError
from clipforge.schemas import SourceHost

log = logging.getLogger(__name__)

ALLOWED_EXTRACTORS: tuple[str, ...] = ("twitch:vod", "twitch:clips")

YtdlFactory = Callable[[dict[str, object]], Any]


class _YtdlLogger:
    """Route yt-dlp messages into logging (yt-dlp sends info lines to `debug`)."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def first_seconds(seconds: int) -> Callable[[dict[str, Any], Any], list[dict[str, float]]]:
    """yt-dlp `download_ranges` callback: only [0, seconds]."""

    def ranges(info: dict[str, Any], ydl: Any) -> list[dict[str, float]]:
        return [{"start_time": 0.0, "end_time": float(seconds)}]

    return ranges


def ytdl_options(out_dir: Path, stem: str, *, max_seconds: int | None = None) -> dict[str, object]:
    """Pure: the yt-dlp options we use. Single item, Twitch extractors only, no playlists, quiet.

    `max_seconds`: download only the first `max_seconds` of the video."""
    opts: dict[str, object] = {
        "outtmpl": {"default": str(out_dir / f"{stem}.%(ext)s")},
        # Twitch serves muxed HLS renditions; "best" needs no ffmpeg merge step.
        "format": "best[ext=mp4]/best",
        "allowed_extractors": list(ALLOWED_EXTRACTORS),
        "noplaylist": True,
        "restrictfilenames": True,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _YtdlLogger(),
    }
    if max_seconds is not None:
        opts["download_ranges"] = first_seconds(max_seconds)
    return opts


def channel_of(info: dict[str, Any]) -> str:
    """Lowercased channel login of a Twitch VOD or clip result ('' when yt-dlp did not report it)."""
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if "clip" in extractor:
        return str(info.get("channel") or "").strip().lower()
    return str(info.get("uploader_id") or "").strip().lower()


def check_channel(info: dict[str, Any], channels: Sequence[str]) -> None:
    """Refuse a result whose channel is not one of `channels` (no-op when `channels` is empty)."""
    if not channels:
        return
    found = channel_of(info)
    if found not in {c.lower() for c in channels}:
        raise ComplianceError(
            1, f"Twitch video belongs to channel {found or '(unknown)'!r}; this campaign allows only {list(channels)}"
        )


def _default_factory(opts: dict[str, object]) -> Any:
    import yt_dlp  # type: ignore[import-untyped]

    return yt_dlp.YoutubeDL(opts)


def download(
    url: str,
    out_dir: Path,
    stem: str,
    *,
    ytdl_factory: YtdlFactory | None = None,
    channels: Sequence[str] = (),
    max_minutes: int | None = None,
    disk_check: Callable[[dict[str, Any]], None] | None = None,
) -> Path:
    """Download one Twitch VOD or clip to `out_dir/<stem>.<ext>`; return the path.

    With `channels`, the metadata is fetched first (no media) and the download is refused unless the video belongs to
    one of those channel logins and is not still live. With `max_minutes`, only the first N minutes are fetched.
    With `disk_check`, the metadata is fetched first too and passed to it (it raises to refuse the download)."""
    from clipforge.ingest.download import DownloadError

    if compliance.classify_source_host(url) is not SourceHost.TWITCH:
        raise ComplianceError(1, "the Twitch handler only downloads twitch.tv URLs")
    out_dir.mkdir(parents=True, exist_ok=True)
    factory = ytdl_factory or _default_factory
    max_seconds = max_minutes * 60 if max_minutes else None
    from clipforge.ingest import watchdog

    info: Any = None
    for attempt in range(watchdog.STALL_RETRIES + 1):
        guard = DownloadGuard()
        opts = ytdl_options(out_dir, stem, max_seconds=max_seconds)
        opts["progress_hooks"] = [guard.hook]
        try:
            with factory(opts) as ydl:
                if attempt == 0 and (channels or disk_check is not None):
                    meta = ydl.extract_info(url, download=False)
                    if not isinstance(meta, dict):
                        raise DownloadError("yt-dlp returned no info")
                    check_channel(meta, channels)
                    if meta.get("is_live"):
                        raise DownloadError("this Twitch VOD is still live (recording); download it after the stream")
                    if disk_check is not None:
                        disk_check(meta)
                info = ydl.extract_info(url, download=True)
        except (ComplianceError, DownloadError):
            raise
        except Exception as exc:  # yt_dlp.utils.DownloadError / ExtractorError and network errors
            if isinstance(guard.error, watchdog.StepStalled) and attempt < watchdog.STALL_RETRIES:
                log.warning("Twitch download stalled; retrying (%d/%d)", attempt + 1, watchdog.STALL_RETRIES)
                continue
            if guard.error is not None:  # raised from our progress hook (yt-dlp may wrap it)
                raise guard.error from exc
            raise DownloadError(f"Twitch download failed: {type(exc).__name__}: {exc}") from exc
        break

    if not isinstance(info, dict):
        raise DownloadError("yt-dlp returned no info")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    path = _downloaded_path(info, out_dir, stem)
    if not extractor.startswith("twitch"):
        if path is not None:
            path.unlink(missing_ok=True)
        raise ComplianceError(1, f"yt-dlp used extractor {extractor!r}; only Twitch is allowed")
    if channels:
        try:
            check_channel(info, channels)
        except ComplianceError:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
    if path is None:
        raise DownloadError("yt-dlp finished but no file was written")
    return path


class DownloadGuard:
    """yt-dlp progress hook: StageTimeout at the download stage's active-time ceiling, StepStalled when the byte
    count has not grown for watchdog.STALL_S of active time (a dead socket is caught sooner by `socket_timeout`)."""

    def __init__(self) -> None:
        from clipforge.ingest import watchdog

        self._wd = watchdog
        self.error: Exception | None = None
        self._bytes = -1
        self._since = watchdog.CLOCK.now()

    def hook(self, d: dict[str, Any]) -> None:
        wd = self._wd
        try:
            wd.check()
        except wd.StageTimeout as exc:
            self.error = exc
            raise
        got = d.get("downloaded_bytes")
        now = wd.CLOCK.now()
        if isinstance(got, int | float) and got != self._bytes:
            self._bytes, self._since = int(got), now
        elif d.get("status") == "downloading" and now - self._since >= wd.STALL_S:
            self.error = wd.StepStalled(f"Twitch download made no progress for {wd.STALL_S / 60:.0f} min")
            raise self.error


def _downloaded_path(info: dict[str, Any], out_dir: Path, stem: str) -> Path | None:
    for item in info.get("requested_downloads") or []:
        fp = item.get("filepath") if isinstance(item, dict) else None
        if fp and Path(fp).is_file():
            return Path(fp)
    candidates = [p for p in out_dir.glob(f"{stem}.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    return candidates[0] if len(candidates) == 1 else None
