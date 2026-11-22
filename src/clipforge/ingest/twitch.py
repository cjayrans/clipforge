"""Twitch VOD / clip download via yt-dlp (PRD F2, COMPLIANCE #1).

Only `ingest.download.download_source` calls this, after `compliance.assert_registered_source` has confirmed the URL
is registered on an active campaign with `allow_stream_download=true`.

yt-dlp facts (checked against the installed yt-dlp 2026.10.26 source, `YoutubeDL` docstring and
`_load_extractors`; README https://github.com/yt-dlp/yt-dlp):
- `allowed_extractors` is a list of regexes matched with `re.fullmatch` (case-insensitive) against extractor names, so
  `["twitch:vod", "twitch:clips"]` loads exactly those two extractors and nothing else (no generic extractor).
- The downloaded file is `info["requested_downloads"][0]["filepath"]`.
Twitch VODs expire (up to 60 days) and clips are <= 60 s: register early, download once, keep the hash.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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


def ytdl_options(out_dir: Path, stem: str) -> dict[str, object]:
    """Pure: the yt-dlp options we use. Single item, Twitch extractors only, no playlists, quiet."""
    return {
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


def _default_factory(opts: dict[str, object]) -> Any:
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)  # type: ignore[arg-type]


def download(url: str, out_dir: Path, stem: str, *, ytdl_factory: YtdlFactory | None = None) -> Path:
    """Download one Twitch VOD or clip to `out_dir/<stem>.<ext>`; return the path."""
    from clipforge.ingest.download import DownloadError

    if compliance.classify_source_host(url) is not SourceHost.TWITCH:
        raise ComplianceError(1, "the Twitch handler only downloads twitch.tv URLs")
    out_dir.mkdir(parents=True, exist_ok=True)
    factory = ytdl_factory or _default_factory
    try:
        with factory(ytdl_options(out_dir, stem)) as ydl:
            info = ydl.extract_info(url, download=True)
    except ComplianceError:
        raise
    except Exception as exc:  # yt_dlp.utils.DownloadError / ExtractorError and network errors
        raise DownloadError(f"Twitch download failed: {type(exc).__name__}: {exc}") from exc

    if not isinstance(info, dict):
        raise DownloadError("yt-dlp returned no info")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    path = _downloaded_path(info, out_dir, stem)
    if not extractor.startswith("twitch"):
        if path is not None:
            path.unlink(missing_ok=True)
        raise ComplianceError(1, f"yt-dlp used extractor {extractor!r}; only Twitch is allowed")
    if path is None:
        raise DownloadError("yt-dlp finished but no file was written")
    return path


def _downloaded_path(info: dict[str, Any], out_dir: Path, stem: str) -> Path | None:
    for item in info.get("requested_downloads") or []:
        fp = item.get("filepath") if isinstance(item, dict) else None
        if fp and Path(fp).is_file():
            return Path(fp)
    candidates = [p for p in out_dir.glob(f"{stem}.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    return candidates[0] if len(candidates) == 1 else None
