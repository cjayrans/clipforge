"""Gameplay crop path, AutoFlip-style (pure; layouts, feat/reframe).

Input: per-sample motion profiles of one shot (`render.saliency.MotionSample`: hot-pixel mass per column, sums to 1),
the source width and the crop width. Output: a `PathPlan`, one camera mode per shot:

- **stationary** (the default): the centre crop. When the salient motion holds still off-centre for most of the shot,
  stationary at its median position instead.
- **tracking**: only when confident salient motion (concentrated: column spread < `CONFIDENT_SPREAD` x crop
  width) is outside the centre window (`CENTRE_WINDOW` of the crop) in at least `TRACK_MIN_FRAC` of the shot *and*
  it moves (its spread of positions exceeds `MOVING_SPREAD` x crop width).
  Path: hold the last confident target, median filter (`MEDIAN_WIN` samples), dead zone (the camera moves only when
  the target leaves the inner `DEAD_ZONE` of the crop), rate limit at `PAN_HEADROOM` x the max pan speed, moving
  average (`SMOOTH_WIN`), clamp to the frame. Speeds stay under the limit: the limiter, averaging, clamping and the
  piecewise-linear simplification never raise the largest step.
- **fit** (crop layout only): when the salient motion splits into two far-apart spots in most moving samples, a crop
  would lose one of them, so the shot gets the blurred-background fit instead.

Global camera motion (third-person games pan all the time) spreads the hot pixels over the whole width, so it is never
"confident" and never moves the crop.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from clipforge.render.saliency import MotionSample
from clipforge.schemas import CropKeyframe

MAX_PAN_FRAC_S = 0.15  # max pan speed: 15 % of the source width per second (288 px/s on 1920)
PAN_HEADROOM = 0.9  # the planner aims under the limit so integer rounding never crosses it
CONFIDENT_SPREAD = 0.3
CENTRE_WINDOW = 0.7
DEAD_ZONE = 0.6
TRACK_MIN_FRAC = 0.3
MOVING_SPREAD = 0.15
STATIONARY_OFFSET_MIN_FRAC = 0.5
SPLIT_MIN_FRAC = 0.5
MEDIAN_WIN = 5
SMOOTH_WIN = 3
SIMPLIFY_PX = 2.0
MIN_TRACK_RANGE_PX = 4


def max_pan_px_s(src_w: int) -> float:
    return MAX_PAN_FRAC_S * src_w


@dataclass(frozen=True)
class SampleStats:
    t: float
    moving: bool
    cx: float  # source px
    spread: float  # source px
    confident: bool
    split: bool


@dataclass(frozen=True)
class PathPlan:
    camera: Literal["stationary", "tracking"]
    x: int  # left edge (stationary), or the first keyframe
    keyframes: list[CropKeyframe] = field(default_factory=list)
    fit: bool = False
    reason: str = ""


def _window_sums(p: Sequence[float], k: int) -> list[float]:
    s = sum(p[:k])
    out = [s]
    for i in range(k, len(p)):
        s += p[i] - p[i - k]
        out.append(s)
    return out


def sample_stats(sample: MotionSample, src_w: int, crop_w: int) -> SampleStats:
    p = sample.profile
    total = sum(p)
    if not p or total <= 0:
        return SampleStats(t=sample.t, moving=False, cx=src_w / 2, spread=float(src_w), confident=False, split=False)
    p = [v / total for v in p]
    n = len(p)
    bin_w = src_w / n
    xs = [(i + 0.5) * bin_w for i in range(n)]
    cx = sum(v * x for v, x in zip(p, xs, strict=True))
    spread = math.sqrt(sum(v * (x - cx) ** 2 for v, x in zip(p, xs, strict=True)))
    k = max(1, min(n, round(crop_w / bin_w)))
    sums = _window_sums(p, k)
    j1 = max(range(len(sums)), key=sums.__getitem__)
    m1 = sums[j1]
    others = [s for j, s in enumerate(sums) if j + k <= j1 or j >= j1 + k]
    m2 = max(others, default=0.0)
    r = crop_w / src_w
    split = 2 * r < 1 and m2 >= 0.3 and m1 < 0.75 and m1 + m2 >= 2 * r + 0.6 * (1 - 2 * r)
    return SampleStats(
        t=sample.t, moving=True, cx=cx, spread=spread, confident=spread < CONFIDENT_SPREAD * crop_w, split=split
    )


def _clamp(v: float, lo: float, hi: float) -> float:
    return min(max(v, lo), hi)


def _median_filter(values: list[float], win: int) -> list[float]:
    half = win // 2
    return [statistics.median(values[max(0, i - half) : i + half + 1]) for i in range(len(values))]


def _moving_average(values: list[float], win: int) -> list[float]:
    half = win // 2
    out = []
    for i in range(len(values)):
        part = values[max(0, i - half) : i + half + 1]
        out.append(sum(part) / len(part))
    return out


def _simplify(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on (t, x): drop points within `tol` px of the line between kept neighbours."""
    if len(points) <= 2:
        return points
    (t0, x0), (t1, x1) = points[0], points[-1]
    worst, idx = -1.0, 0
    for i in range(1, len(points) - 1):
        t, x = points[i]
        line = x0 + (x1 - x0) * (t - t0) / (t1 - t0) if t1 > t0 else x0
        d = abs(x - line)
        if d > worst:
            worst, idx = d, i
    if worst <= tol:
        return [points[0], points[-1]]
    return _simplify(points[: idx + 1], tol)[:-1] + _simplify(points[idx:], tol)


def plan_path(
    samples: Sequence[MotionSample],
    duration_s: float,
    src_w: int,
    crop_w: int,
    *,
    allow_fit: bool,
    max_pan: float,
) -> PathPlan:
    """Camera plan for one shot (`samples` in shot-relative seconds)."""
    lo, hi = crop_w / 2, src_w - crop_w / 2
    centre_x = (src_w - crop_w) // 2
    if crop_w >= src_w:
        return PathPlan(camera="stationary", x=0, reason="crop is as wide as the source")
    stats = [sample_stats(s, src_w, crop_w) for s in sorted(samples, key=lambda s: s.t)]
    n = len(stats)
    if n == 0:
        return PathPlan(camera="stationary", x=centre_x, reason="no motion samples: centre")
    moving = [s for s in stats if s.moving]
    if allow_fit and moving and sum(s.split for s in moving) >= SPLIT_MIN_FRAC * len(moving):
        return PathPlan(camera="stationary", x=centre_x, fit=True, reason="salient action split across the frame")
    conf = [s for s in stats if s.confident]
    half_window = CENTRE_WINDOW * crop_w / 2
    outside = [s for s in conf if abs(s.cx - src_w / 2) > half_window]
    if len(outside) < TRACK_MIN_FRAC * n:
        if len(conf) >= STATIONARY_OFFSET_MIN_FRAC * n and conf:
            med = statistics.median(s.cx for s in conf)
            if abs(med - src_w / 2) > half_window:
                x = round(_clamp(med, lo, hi) - crop_w / 2)
                return PathPlan(camera="stationary", x=x, reason="salient motion holds still off-centre")
        return PathPlan(camera="stationary", x=centre_x, reason="salient motion stays in the centre crop")
    positions = [s.cx for s in conf]
    if len(positions) < 2 or statistics.pstdev(positions) < MOVING_SPREAD * crop_w:
        x = round(_clamp(statistics.median(positions), lo, hi) - crop_w / 2)
        return PathPlan(camera="stationary", x=x, reason="salient motion holds still off-centre")

    # Tracking.
    targets: list[float] = []
    last = _clamp(conf[0].cx, lo, hi)
    for s in stats:
        if s.confident:
            last = _clamp(s.cx, lo, hi)
        targets.append(last)
    targets = _median_filter(targets, MEDIAN_WIN)
    dz = DEAD_ZONE * crop_w / 2
    cam = targets[0]
    path: list[float] = []
    prev_t = stats[0].t
    for t, g in zip((s.t for s in stats), targets, strict=True):
        step = max_pan * PAN_HEADROOM * max(0.0, t - prev_t)
        if g > cam + dz:
            want = g - dz
        elif g < cam - dz:
            want = g + dz
        else:
            want = cam
        cam = _clamp(cam + _clamp(want - cam, -step, step), lo, hi)
        path.append(cam)
        prev_t = t
    path = [_clamp(v, lo, hi) for v in _moving_average(path, SMOOTH_WIN)]
    lefts = [min(max(0, round(v - crop_w / 2)), src_w - crop_w) for v in path]
    if max(lefts) - min(lefts) < MIN_TRACK_RANGE_PX:
        return PathPlan(camera="stationary", x=lefts[0], reason="tracking path barely moves")
    points = [(0.0, float(lefts[0]))]
    points += [(round(s.t, 3), float(x)) for s, x in zip(stats, lefts, strict=True) if s.t > 0]
    end = round(max(duration_s, points[-1][0]), 3)
    if end > points[-1][0]:
        points.append((end, points[-1][1]))
    kept = _simplify(points, SIMPLIFY_PX)
    keyframes = [CropKeyframe(t=t, x=int(x)) for t, x in kept]
    return PathPlan(camera="tracking", x=keyframes[0].x, keyframes=keyframes, reason="salient motion moves")


def max_speed(keyframes: Sequence[CropKeyframe]) -> float:
    """Largest |dx/dt| over consecutive keyframes (px/s)."""
    best = 0.0
    for a, b in zip(keyframes, keyframes[1:], strict=False):
        dt = b.t - a.t
        if dt <= 0:
            if b.x != a.x:
                return math.inf
            continue
        best = max(best, abs(b.x - a.x) / dt)
    return best


def x_expr(keyframes: Sequence[CropKeyframe], fallback_x: int) -> str:
    """ffmpeg expression in `t` (shot seconds) for the crop's left edge: piecewise linear through the keyframes,
    a balanced tree of `if(lt(t,..),..,..)` (depth log2 n), held at the last keyframe afterwards."""
    kf = list(keyframes)
    if len(kf) < 2:
        return str(kf[0].x if kf else fallback_x)

    def seg(a: CropKeyframe, b: CropKeyframe) -> str:
        if a.x == b.x or b.t <= a.t:
            return str(a.x)
        return f"{a.x}+({b.x - a.x})*(t-{a.t:.3f})/{b.t - a.t:.3f}"

    def build(lo: int, hi: int) -> str:
        if hi - lo == 1:
            return seg(kf[lo], kf[lo + 1])
        mid = (lo + hi) // 2
        return f"if(lt(t,{kf[mid].t:.3f}),{build(lo, mid)},{build(mid, hi)})"

    return f"if(gte(t,{kf[-1].t:.3f}),{kf[-1].x},{build(0, len(kf) - 1)})"
