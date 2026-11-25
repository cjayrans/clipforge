"""Reframe planner: per-shot crop for a 9:16 output (ARCHITECTURE §4.4, PRD F5/F16).

Pure core: `plan_shot` and `plan_crops` take face detections (from `render.face_detect` or a test fake) and return
`schemas.CropPlan`. Rules:
- `content_kind == "game"`: `plan_crops` gives every shot `fit_blur` and skips detection (a facecam in a corner must
  not drive a talk-style face crop). Game clips normally use the layouts instead (render.layout, render.clip); this
  path is their `fit` layout.
- Per sampled frame, the largest face is the subject (v1). If too few frames have a face → `fit_blur`.
- If in most face frames the two largest faces' centres are further apart than the crop width → `fit_blur`. The
  second face only counts when its area is at least `SECOND_FACE_MIN_AREA_RATIO` of the largest (a small face in
  the background, a poster or a facecam thumbnail does not split the frame).
- Otherwise `face`: crop x-centre = median of the subject's centres, width = src_h × 9/16 rounded down to even,
  clamped inside the frame.
- Sources already as narrow as 9:16 always get `fit_blur` (nothing to crop).
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from clipforge.render.shots import Shot
from clipforge.schemas import CropMode, CropPlan, ShotCrop

MIN_FACE_FRAME_RATIO = 0.3
MAX_FAR_APART_RATIO = 0.5
SECOND_FACE_MIN_AREA_RATIO = 0.25


@dataclass(frozen=True)
class FaceBox:
    """A detected face in source pixels."""

    x: float
    y: float
    w: float
    h: float
    score: float = 1.0

    @property
    def center_x(self) -> float:
        return self.x + self.w / 2

    @property
    def area(self) -> float:
        return self.w * self.h


FrameFaces = Sequence[FaceBox]
Detector = Callable[[Shot], Sequence[FrameFaces]]


def _even(n: float) -> int:
    return max(2, int(n // 2) * 2)


def crop_width(src_w: int, src_h: int) -> int:
    """Width of a full-height 9:16 crop, even, never wider than the (even) source width."""
    return min(_even(src_h * 9 / 16), src_w - src_w % 2)


def fit_blur(shot: Shot, src_w: int, src_h: int) -> ShotCrop:
    return ShotCrop(start_s=shot.start_s, end_s=shot.end_s, mode=CropMode.FIT_BLUR, x=0, w=src_w, h=src_h)


def plan_shot(
    shot: Shot,
    frames: Sequence[FrameFaces],
    src_w: int,
    src_h: int,
    *,
    min_face_frame_ratio: float = MIN_FACE_FRAME_RATIO,
    max_far_apart_ratio: float = MAX_FAR_APART_RATIO,
) -> ShotCrop:
    w = crop_width(src_w, src_h)
    if w >= src_w - src_w % 2 or not frames:
        return fit_blur(shot, src_w, src_h)

    centers: list[float] = []
    far_apart = 0
    for faces in frames:
        if not faces:
            continue
        ranked = sorted(faces, key=lambda f: f.area, reverse=True)
        centers.append(ranked[0].center_x)
        if (
            len(ranked) > 1
            and ranked[1].area >= SECOND_FACE_MIN_AREA_RATIO * ranked[0].area
            and abs(ranked[0].center_x - ranked[1].center_x) > w
        ):
            far_apart += 1

    if len(centers) < min_face_frame_ratio * len(frames):
        return fit_blur(shot, src_w, src_h)
    if far_apart > max_far_apart_ratio * len(centers):
        return fit_blur(shot, src_w, src_h)

    center = statistics.median(centers)
    x = int(round(center - w / 2))
    x = min(max(0, x), src_w - w)
    return ShotCrop(start_s=shot.start_s, end_s=shot.end_s, mode=CropMode.FACE, x=x, w=w, h=src_h)


def plan_crops(
    shots: Sequence[Shot],
    src_w: int,
    src_h: int,
    content_kind: Literal["talk", "game"],
    detector: Detector | None,
) -> CropPlan:
    """Crop plan for a clip. `detector` returns sampled frames of faces for a shot; None means no face detection
    is available, which falls back to `fit_blur` everywhere (PRD F16)."""
    if not shots:
        raise ValueError("no shots to plan")
    if content_kind == "game" or detector is None:
        crops = [fit_blur(s, src_w, src_h) for s in shots]
    else:
        crops = [plan_shot(s, detector(s), src_w, src_h) for s in shots]
    return CropPlan(src_w=src_w, src_h=src_h, shots=crops)
