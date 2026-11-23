"""Telegram preview: the clip itself when small enough, else a 480p proxy (ARCHITECTURE §4.6).

Verified 2026-11-22 at https://core.telegram.org/bots/api ("Sending files"): files uploaded by multipart/form-data
are limited to "50 MB for other files" (the bot uploads previews this way). We use 50 × 10^6 bytes to stay under
either reading of "MB".
"""

from __future__ import annotations

from pathlib import Path

from clipforge.render.encode import RenderError, Runner, run_ffmpeg

TELEGRAM_VIDEO_LIMIT_BYTES = 50_000_000
PROXY_WIDTH = 480  # 480×854 for a 9:16 clip


def proxy_args(clip: Path, out: Path, ffmpeg: str = "ffmpeg") -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", str(clip),
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", f"scale={PROXY_WIDTH}:-2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(out),
    ]  # fmt: skip


def preview_for_telegram(clip: Path, limit_bytes: int = TELEGRAM_VIDEO_LIMIT_BYTES, run: Runner = run_ffmpeg) -> Path:
    """Path to send with sendVideo: `clip` if it fits, else `<clip>.preview.mp4` (rendered once, reused)."""
    if clip.stat().st_size <= limit_bytes:
        return clip
    out = clip.with_suffix(".preview.mp4")
    if not out.is_file() or out.stat().st_mtime < clip.stat().st_mtime:
        run(proxy_args(clip, out))
    if out.stat().st_size > limit_bytes:
        raise RenderError(f"preview proxy is still over {limit_bytes} bytes: {out}")
    return out
