"""AutoFlip-style gameplay crop path (render/crop_path.py) and the motion profiles it reads (render/saliency.py)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from clipforge.render import crop_path as cp
from clipforge.render.saliency import MotionSample, profiles_from_frames, samples_from_profiles
from clipforge.schemas import CropKeyframe

SRC_W = 1920
CROP_W = 606  # full-height 9:16 crop of 1080p
BINS = 192
MAX_PAN = cp.max_pan_px_s(SRC_W)


def spot(x_px: float, width_px: float = 60) -> tuple[float, ...]:
    """A concentrated hot spot centred at x_px (source pixels)."""
    centre, half = x_px / SRC_W * BINS, max(1.0, width_px / SRC_W * BINS / 2)
    p = [1.0 if abs(i + 0.5 - centre) <= half else 0.0 for i in range(BINS)]
    total = sum(p)
    return tuple(v / total for v in p)


def uniform() -> tuple[float, ...]:
    return tuple(1 / BINS for _ in range(BINS))


def two_spots(a: float, b: float) -> tuple[float, ...]:
    return tuple((x + y) / 2 for x, y in zip(spot(a), spot(b), strict=True))


def samples(profiles: list[tuple[float, ...]], fps: float = 4.0) -> list[MotionSample]:
    return [MotionSample(t=round(i / fps, 3), profile=p) for i, p in enumerate(profiles)]


def plan(profiles: list[tuple[float, ...]], *, allow_fit: bool = True, duration: float | None = None) -> cp.PathPlan:
    d = duration if duration is not None else len(profiles) / 4.0
    return cp.plan_path(samples(profiles), d, SRC_W, CROP_W, allow_fit=allow_fit, max_pan=MAX_PAN)


CENTRE = (SRC_W - CROP_W) // 2


def test_no_samples_is_a_stationary_centre_crop() -> None:
    p = plan([])
    assert (p.camera, p.x, p.fit) == ("stationary", CENTRE, False)


def test_global_camera_motion_never_moves_the_crop() -> None:
    p = plan([uniform()] * 80)
    assert (p.camera, p.x, p.fit) == ("stationary", CENTRE, False)
    assert not cp.sample_stats(MotionSample(0, uniform()), SRC_W, CROP_W).confident


def test_action_in_the_centre_stays_stationary() -> None:
    p = plan([spot(960 + (i % 5 - 2) * 30) for i in range(80)])
    assert (p.camera, p.x) == ("stationary", CENTRE)


def test_action_holding_still_off_centre_is_a_stationary_offset_crop() -> None:
    p = plan([spot(300)] * 80)
    assert p.camera == "stationary"
    assert p.x == 0  # as far left as the frame allows (300 - 303 < 0)
    assert not p.keyframes


def test_moving_action_is_tracked_within_the_speed_limit_and_the_frame() -> None:
    # the subject drifts from the left edge to the right edge over 20 s
    profiles = [spot(200 + (1520 * i / 79)) for i in range(80)]
    p = plan(profiles)
    assert p.camera == "tracking"
    assert len(p.keyframes) >= 2
    assert p.keyframes[0].t == 0.0
    assert all(0 <= k.x <= SRC_W - CROP_W for k in p.keyframes)
    assert cp.max_speed(p.keyframes) <= MAX_PAN
    assert p.keyframes[-1].x > p.keyframes[0].x + 500  # it really followed


def test_tracking_has_no_jitter() -> None:
    """A subject moving steadily right with noisy detections: the camera never reverses."""
    rng = np.random.default_rng(3)
    profiles = [spot(300 + 15 * i + float(rng.normal(0, 40))) for i in range(80)]
    p = plan(profiles)
    assert p.camera == "tracking"
    steps = [b.x - a.x for a, b in zip(p.keyframes, p.keyframes[1:], strict=False)]
    assert all(s >= 0 for s in steps), steps


def test_speed_limit_holds_for_a_jump() -> None:
    profiles = [spot(200)] * 20 + [spot(1700)] * 60
    p = plan(profiles)
    assert p.camera == "tracking"
    assert cp.max_speed(p.keyframes) <= MAX_PAN


def test_split_action_falls_back_to_fit_only_when_allowed() -> None:
    profiles = [two_spots(250, 1650)] * 40
    assert plan(profiles).fit is True
    assert plan(profiles, allow_fit=False).fit is False


def test_expression_follows_the_keyframes() -> None:
    kf = [CropKeyframe(t=0.0, x=100), CropKeyframe(t=2.0, x=300), CropKeyframe(t=5.0, x=300), CropKeyframe(t=6.0, x=0)]
    expr = cp.x_expr(kf, 0)

    def ev(t: float) -> float:
        env: dict[str, Any] = {
            "t": t,
            "if_": lambda c, a, b: a if c else b,
            "lt": lambda a, b: a < b,
            "gte": lambda a, b: a >= b,
        }
        return float(eval(expr.replace("if(", "if_("), env))  # noqa: S307 - our own expression, test only

    assert ev(0) == pytest.approx(100)
    assert ev(1) == pytest.approx(200)
    assert ev(3.5) == pytest.approx(300)
    assert ev(5.5) == pytest.approx(150)
    assert ev(7) == pytest.approx(0)  # held at the last keyframe
    assert "[" not in expr
    assert "]" not in expr


def test_expression_tree_is_balanced() -> None:
    kf = [CropKeyframe(t=i * 0.25, x=i) for i in range(257)]
    expr = cp.x_expr(kf, 0)
    depth = max_depth = 0
    for ch in expr:
        depth += ch == "("
        depth -= ch == ")"
        max_depth = max(max_depth, depth)
    assert max_depth < 40  # ~3 levels per if, log2(256) = 8 ifs deep
    assert cp.x_expr([], 42) == "42"
    assert cp.x_expr([CropKeyframe(t=0, x=7)], 42) == "7"


def test_max_speed() -> None:
    assert cp.max_speed([CropKeyframe(t=0, x=0), CropKeyframe(t=2, x=100)]) == 50
    assert math.isinf(cp.max_speed([CropKeyframe(t=1, x=0), CropKeyframe(t=1, x=5)]))


# --- motion profiles ----------------------------------------------------------------------------------------


def _frames_with_square(xs: list[int], h: int = 108, w: int = 192) -> Any:
    frames = np.full((len(xs), h, w), 40, dtype=np.uint8)
    for i, x in enumerate(xs):
        frames[i, 40:60, x : x + 12] = 230
    return frames


def test_profiles_peak_where_things_move() -> None:
    profiles = profiles_from_frames(_frames_with_square([20, 24, 28, 32]))
    assert len(profiles) == 3
    for p in profiles:
        assert sum(p) == pytest.approx(1.0)
        peak = max(range(len(p)), key=p.__getitem__)
        assert 15 <= peak <= 50


def test_static_frames_and_masked_motion_have_no_profile() -> None:
    still = np.full((4, 108, 192), 90, dtype=np.uint8)
    assert profiles_from_frames(still) == [(), (), ()]
    masked = profiles_from_frames(_frames_with_square([20, 24, 28, 32]), mask=(0, 0, 80, 108))
    assert masked == [(), (), ()]


def test_samples_are_stamped_at_the_later_frame() -> None:
    s = samples_from_profiles([(), (1.0,)], fps=4.0)
    assert [x.t for x in s] == [0.25, 0.5]
