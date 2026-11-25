"""Salient motion for the gameplay crop (layouts, feat/reframe).

One ffmpeg pass per clip: `fps=MOTION_FPS`, scaled to `MOTION_W` px wide, grey, raw bytes on stdout (cheap: a 60 s
clip is ~240 frames of 192x108). `profiles_from_frames` (pure, numpy) turns consecutive frames into one column profile
each: absolute difference, a small blur, the facecam box masked out (the streamer's own movement must not pull the
crop), then only the hottest `100 - HOT_PCT` % of pixels (and only above `MIN_DIFF`) count. The profile is that hot
mass per column, normalised to 1; empty when nothing moves. `render.crop_path` plans the camera from them.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clipforge.schemas import PxBox

MOTION_FPS = 4.0
MOTION_W = 192
HOT_PCT = 90.0
MIN_DIFF = 6.0  # grey levels; below this a pixel did not really change
MIN_HOT_FRAC = 0.005  # fewer hot pixels than this share of the frame = no motion
TIMEOUT_S = 600


@dataclass(frozen=True)
class MotionSample:
    t: float  # seconds from the start of the analysed range (the clip, or a shot once split)
    profile: tuple[float, ...]  # hot-pixel mass per column, sums to 1; () = no salient motion


def small_size(src_w: int, src_h: int) -> tuple[int, int]:
    w = MOTION_W
    h = max(2, round(src_h * w / src_w / 2) * 2)
    return w, h


def profiles_from_frames(frames: Any, mask: tuple[int, int, int, int] | None = None) -> list[tuple[float, ...]]:
    """Pure: one column profile per consecutive frame pair of an (n, H, W) uint8 array. `mask` = (x0, y0, x1, y1)
    in small-frame pixels, ignored."""
    import cv2
    import numpy as np

    out: list[tuple[float, ...]] = []
    if len(frames) < 2:
        return out
    prev = frames[0].astype(np.int16)
    for frame in frames[1:]:
        cur = frame.astype(np.int16)
        diff = cv2.GaussianBlur(np.abs(cur - prev).astype(np.float32), (5, 5), 0)
        prev = cur
        if mask is not None:
            x0, y0, x1, y1 = mask
            diff[max(0, y0) : max(0, y1), max(0, x0) : max(0, x1)] = 0.0
        thr = max(MIN_DIFF, float(np.percentile(diff, HOT_PCT)))
        hot = np.where(diff >= thr, diff, 0.0)
        if np.count_nonzero(hot) < MIN_HOT_FRAC * hot.size:
            out.append(())
            continue
        cols = hot.sum(axis=0)
        total = float(cols.sum())
        out.append(tuple(float(c) / total for c in cols) if total > 0 else ())
    return out


def samples_from_profiles(profiles: Sequence[tuple[float, ...]], fps: float = MOTION_FPS) -> list[MotionSample]:
    """Profile i compares frames i and i+1, so it is stamped at (i + 1) / fps."""
    return [MotionSample(t=round((i + 1) / fps, 3), profile=p) for i, p in enumerate(profiles)]


def grab_gray(path: Path, start_s: float, end_s: float, src_w: int, src_h: int) -> Any:
    """(n, H, W) uint8 grey frames of [start_s, end_s] at MOTION_FPS."""
    import numpy as np

    w, h = small_size(src_w, src_h)
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
        "-ss", f"{start_s:.3f}", "-t", f"{max(0.1, end_s - start_s):.3f}", "-i", str(path),
        "-an", "-sn", "-dn", "-vf", f"fps={MOTION_FPS:g},scale={w}:{h},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]  # fmt: skip
    try:
        done = subprocess.run(args, check=True, capture_output=True, timeout=TIMEOUT_S)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise OSError(f"motion pass failed: {type(exc).__name__}") from exc
    n = len(done.stdout) // (w * h)
    return np.frombuffer(done.stdout[: n * w * h], dtype=np.uint8).reshape(n, h, w)


def motion_samples(
    path: Path, start_s: float, end_s: float, src_w: int, src_h: int, mask: PxBox | None = None
) -> list[MotionSample]:
    """Motion samples of [start_s, end_s] (times relative to start_s), facecam box masked."""
    w, h = small_size(src_w, src_h)
    small_mask = None
    if mask is not None:
        sx, sy = w / src_w, h / src_h
        small_mask = (
            int(mask.x * sx) - 1,
            int(mask.y * sy) - 1,
            int((mask.x + mask.w) * sx) + 2,
            int((mask.y + mask.h) * sy) + 2,
        )
    frames = grab_gray(path, start_s, end_s, src_w, src_h)
    return samples_from_profiles(profiles_from_frames(frames, small_mask))
