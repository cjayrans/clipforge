"""ffprobe wrapper: `probe(path) -> ProbeResult` (pure parser `parse_probe` + subprocess runner).

Uses `ffprobe -v error -print_format json -show_format -show_streams <path>` (standard ffprobe JSON writer:
`streams[].codec_type/codec_name/width/height/avg_frame_rate/sample_rate/channels`, `format.duration/size`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

PROBE_TIMEOUT_S = 60


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    size_bytes: int
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    audio_streams: int
    video_streams: int


def _rate(value: str | None) -> float | None:
    if not value or value in ("0/0", "0"):
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def parse_probe(data: dict[str, Any]) -> ProbeResult:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    v = video[0] if video else {}
    a = audio[0] if audio else {}
    duration = fmt.get("duration") or v.get("duration") or 0
    return ProbeResult(
        duration_s=float(duration),
        size_bytes=int(fmt.get("size") or 0),
        width=int(v["width"]) if "width" in v else None,
        height=int(v["height"]) if "height" in v else None,
        fps=_rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate")),
        video_codec=v.get("codec_name"),
        audio_codec=a.get("codec_name"),
        audio_streams=len(audio),
        video_streams=len(video),
    )


def ffprobe_json(path: Path | str) -> dict[str, Any]:
    exe = shutil.which("ffprobe")
    if exe is None:
        raise ProbeError("ffprobe not found on PATH")
    cmd = [exe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except subprocess.CalledProcessError as exc:
        raise ProbeError(f"ffprobe failed for {path}: {exc.stderr.strip()[:500]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out for {path}") from exc
    result: dict[str, Any] = json.loads(out.stdout)
    return result


def probe(path: Path | str, runner: Callable[[Path | str], dict[str, Any]] = ffprobe_json) -> ProbeResult:
    return parse_probe(runner(path))
