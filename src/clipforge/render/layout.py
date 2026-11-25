"""Layouts of the 1080x1920 frame (pure; feat/reframe).

- **stacked**: the facecam box (source) scaled into a 1080x`FACECAM_PANEL_H` panel at the top, a gameplay crop
  (aspect 1080:`GAMEPLAY_PANEL_H`, full source height) scaled into the panel below.
- **crop**: a full-height 9:16 gameplay crop.
- **fit**: the whole source over a blurred copy of itself (PRD F16, the old default).

`candidates` gives the order a render tries them in: a campaign's `layout: auto` tries stacked (only when a facecam
is known), then crop, then fit; a set layout is tried alone. The renderer (`render.clip`) moves on when a layout's
prerequisites fail (no face in the facecam box over the clip, a source too narrow to crop) or when the hook, the "Ad"
label, the captions and a campaign overlay cannot all be placed clear of each other, the platform UI and the face
(`render.campaign_overlay.place_texts`). Nothing here is campaign-specific.
"""

from __future__ import annotations

from typing import Literal

from clipforge.schemas import LayoutName, PxBox, Rect, RenderedLayout

FRAME_W, FRAME_H = 1080, 1920
FACECAM_PANEL_H = 640
GAMEPLAY_PANEL_H = FRAME_H - FACECAM_PANEL_H
STACKED_MIN_FACE_RATIO = 0.6  # share of the clip's sampled facecam crops that must show a face
FACE_PAD_FRAC = 0.2  # the face keep-out grows by 20 % of the face size on each side (hair, chin, head turns)


class LayoutUnavailable(ValueError):
    """This layout cannot be used for this clip (the reason is recorded in the manifest)."""


def candidates(requested: LayoutName, content_kind: Literal["talk", "game"], has_facecam: bool) -> list[RenderedLayout]:
    if content_kind == "talk":
        # Talk footage keeps the face-crop planner (render.reframe); "crop" means that planner, "fit" forces fit.
        return ["fit"] if requested == "fit" else ["crop"]
    if requested == "auto":
        return [*(["stacked"] if has_facecam else []), "crop", "fit"]
    return [requested]


def _even(n: float) -> int:
    return max(2, int(n // 2) * 2)


def gameplay_crop(layout: RenderedLayout, src_w: int, src_h: int) -> tuple[int, int]:
    """(w, h) in source px of the gameplay crop: full height at the panel's aspect, narrower than the source."""
    aspect = FRAME_W / GAMEPLAY_PANEL_H if layout == "stacked" else FRAME_W / FRAME_H
    even_w, even_h = src_w - src_w % 2, src_h - src_h % 2
    w, h = _even(even_h * aspect), even_h
    if w > even_w:
        w, h = even_w, min(even_h, _even(even_w / aspect))
    return w, h


def croppable(layout: RenderedLayout, src_w: int, src_h: int) -> bool:
    """The crop is narrower than the source (there is something to crop)."""
    w, _ = gameplay_crop(layout, src_w, src_h)
    return w < src_w - src_w % 2


def facecam_panel() -> Rect:
    return Rect(x0=0, y0=0, x1=FRAME_W, y1=FACECAM_PANEL_H)


def face_rect_out(face_in_crop: PxBox, facecam: PxBox) -> Rect:
    """The face (pixels of the facecam crop) in the output frame, padded by FACE_PAD_FRAC, clipped to the panel."""
    sx, sy = FRAME_W / facecam.w, FACECAM_PANEL_H / facecam.h
    px, py = FACE_PAD_FRAC * face_in_crop.w, FACE_PAD_FRAC * face_in_crop.h
    x0 = max(0, int((face_in_crop.x - px) * sx))
    y0 = max(0, int((face_in_crop.y - py) * sy))
    x1 = min(FRAME_W, int((face_in_crop.x + face_in_crop.w + px) * sx) + 1)
    y1 = min(FACECAM_PANEL_H, int((face_in_crop.y + face_in_crop.h + py) * sy) + 1)
    return Rect(x0=x0, y0=y0, x1=max(x1, x0 + 1), y1=max(y1, y0 + 1))
