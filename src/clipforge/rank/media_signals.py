"""Per-second loudness and frame-difference motion for a whole source, with ffmpeg (one cheap pass each).

Argument builders and parsers are pure; `extract` takes the injectable `media.Runner`, so unit tests need no ffmpeg.
Both passes print filter metadata to stdout (`metadata`/`ametadata` with `file=-`), which the runner returns:

    frame:0    pts:0       pts_time:0
    lavfi.astats.Overall.RMS_level=-23.4

- Loudness: mono 8 kHz audio cut into 1 s frames (`asetnsamples`), `astats` RMS level per frame (reset every frame).
- Motion: `signalstats` YDIF (mean absolute luma difference to the previous decoded frame) on a 160x90 copy. By
  default only keyframes are decoded (`-skip_frame nokey`, about one every 2 s on Twitch VODs), which is far cheaper
  than decoding every frame; `motion_keyframes_only: false` samples at `motion_sample_fps` instead.

A failure of either pass is not fatal: the ranker runs on the transcript signals alone and the gates that need the
missing signal record "not evaluated".
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from clipforge.ingest import media
from clipforge.rank.profile import SignalRules

log = logging.getLogger(__name__)

LOUDNESS_RATE = 8000
RMS_KEY = "lavfi.astats.Overall.RMS_level"
YDIF_KEY = "lavfi.signalstats.YDIF"
MOTION_SCALE = "160:90"
DB_FLOOR = -90.0


@dataclass
class MediaSignals:
    loudness_db: list[float] | None = None  # index = second
    motion: list[tuple[float, float]] | None = None  # (pts seconds, YDIF)
    problems: list[str] = field(default_factory=list)


def loudness_args(src: Path | str) -> list[str]:
    af = (
        f"asetnsamples=n={LOUDNESS_RATE}:p=0,"
        "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
        f"ametadata=mode=print:key={RMS_KEY}:file=-"
    )
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "error",
        "-i", str(src),
        "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(LOUDNESS_RATE),
        "-af", af,
        "-f", "null", "-",
    ]  # fmt: skip


def motion_args(src: Path | str, rules: SignalRules) -> list[str]:
    stats = f"scale={MOTION_SCALE},format=yuv420p,signalstats,metadata=mode=print:key={YDIF_KEY}:file=-"
    if rules.motion_keyframes_only:
        pre, vf = ["-skip_frame", "nokey"], stats
    else:
        pre, vf = [], f"fps={rules.motion_sample_fps:g}," + stats
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "error",
        *pre,
        "-i", str(src),
        "-map", "0:v:0", "-an",
        "-vf", vf,
        "-f", "null", "-",
    ]  # fmt: skip


def parse_metadata(stdout: str, key: str) -> list[tuple[float, float]]:
    """(pts_time, value) pairs from `metadata=mode=print` output. `-inf`/`nan` values become DB_FLOOR."""
    out: list[tuple[float, float]] = []
    pts: float | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("frame:"):
            pts = None
            for part in line.split():
                if part.startswith("pts_time:"):
                    try:
                        pts = float(part.split(":", 1)[1])
                    except ValueError:
                        pts = None
        elif line.startswith(key + "=") and pts is not None:
            raw = line.split("=", 1)[1]
            try:
                value = float(raw)
            except ValueError:
                continue
            out.append((pts, value if math.isfinite(value) else DB_FLOOR))
            pts = None
    return out


def per_second(samples: Sequence[tuple[float, float]]) -> list[float]:
    """Loudness samples (one per 1 s frame) as a list indexed by whole second; gaps repeat the floor."""
    if not samples:
        return []
    n = int(math.floor(max(t for t, _ in samples))) + 1
    out = [DB_FLOOR] * n
    for t, v in samples:
        k = int(math.floor(t + 1e-6))
        if 0 <= k < n:
            out[k] = max(v, DB_FLOOR)
    return out


def extract(src: Path, rules: SignalRules, runner: media.Runner = media.run) -> MediaSignals:
    """Both passes; a failed pass is reported in `problems` and leaves its field None."""
    signals = MediaSignals()
    try:
        loud = per_second(parse_metadata(runner(loudness_args(src)), RMS_KEY))
        signals.loudness_db = loud or None
        if not loud:
            signals.problems.append("loudness: ffmpeg printed no levels")
    except media.MediaToolError as exc:
        signals.problems.append(f"loudness: {exc}")
    try:
        motion = parse_metadata(runner(motion_args(src, rules)), YDIF_KEY)
        signals.motion = motion or None
        if not motion:
            signals.problems.append("motion: ffmpeg printed no frame differences")
    except media.MediaToolError as exc:
        signals.problems.append(f"motion: {exc}")
    for problem in signals.problems:
        log.warning("moment signals for %s: %s", src.name, problem)
    return signals
