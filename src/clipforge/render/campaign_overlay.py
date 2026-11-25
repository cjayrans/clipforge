"""Campaign overlay image: placement, clearance checks and the "Ad" label position (COMPLIANCE invariant 3).

A campaign may supply an image (PNG with alpha, e.g. a streamer's channel banner) that must be visible on every
clip (`schemas.CampaignOverlay`). Generic for every campaign; nothing here knows about any one campaign.

Geometry is in the 1080x1920 output frame. "Opaque rect" = the bounding box of the image's visible pixels
(alpha > OPAQUE_ALPHA) after scaling; transparent margins may overlap anything.

Rules (each one raises `OverlayPlacementError`; nothing here ever shrinks or moves the overlay on its own):
- the opaque rect lies inside the frame and outside the platform UI zones (top 150 px, right 15 %, bottom 20 %);
- it misses the caption band (the 62–70 % safe zone, widened by the largest caption box) and the hook box;
- the "Ad" label is drawn at the first fixed spot clear of the opaque rect (default top-left). If no spot is clear,
  the render is refused.
The overlay is composited before the `ass` filter, so the label, hook and captions are drawn on top of it anyway.
"""

from __future__ import annotations

import functools
import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from clipforge.config import Style
from clipforge.render.captions import SAFE_ZONE_PCT
from clipforge.render.overlays import AD_LABEL_POS, HOOK_MAX_LINES, HOOK_Y_PCT
from clipforge.schemas import CampaignOverlay, Rect

FRAME_W, FRAME_H = 1080, 1920
OPAQUE_ALPHA = 16  # alpha above this counts as visible

# Platform UI zones a campaign overlay must stay out of (TikTok: status bar/tabs on top, action rail on the right,
# caption/description and music bar at the bottom). Captions are kept out of them by `captions.SAFE_ZONE_PCT`.
UI_TOP_PX = 150
UI_RIGHT_PCT = 15.0
UI_BOTTOM_PCT = 20.0

# "Ad" label box estimate (AdLabel style: size 48, BorderStyle 3 with outline 10 as padding).
AD_LABEL_SIZE = 48
AD_LABEL_PAD = 10
AD_LABEL_CHAR_W = 0.75  # generous average glyph width per point of font size
MARGIN_PX = 8  # gap kept between the overlay's visible pixels and anything it must not touch

# Where the "Ad" label may go, in order of preference: (ASS alignment, x, y). 7 = top-left anchor, 9 = top-right,
# 8 = top-centre. All are near the top edge, where the label is expected.
AD_LABEL_SPOTS: tuple[tuple[int, int, int], ...] = (
    (7, AD_LABEL_POS[0], AD_LABEL_POS[1]),
    (9, FRAME_W - AD_LABEL_POS[0], AD_LABEL_POS[1]),
    (8, FRAME_W // 2, AD_LABEL_POS[1]),
)


class OverlayPlacementError(ValueError):
    """The campaign overlay cannot be placed without covering something it must not cover."""


@dataclass(frozen=True)
class ImageInfo:
    width: int
    height: int
    opaque: tuple[int, int, int, int]  # x0, y0, x1, y1 in image pixels (x1/y1 exclusive)
    sha256: str


@dataclass(frozen=True)
class OverlayPlacement:
    path: Path
    rect: Rect  # where the scaled image goes
    opaque_rect: Rect  # its visible part
    width: int  # scaled size passed to ffmpeg
    height: int
    sha256: str


@dataclass(frozen=True)
class AdLabelPlacement:
    alignment: int
    x: int
    y: int
    rect: Rect


def read_image(path: Path) -> ImageInfo:
    """Size, visible bounding box and SHA-256 of a PNG with an alpha channel (cached per path, size and mtime)."""
    st = path.stat()
    return _read_image(str(path), st.st_size, st.st_mtime_ns)


@functools.lru_cache(maxsize=16)
def _read_image(path: str, _size: int, _mtime_ns: int) -> ImageInfo:
    import cv2
    import numpy as np

    data = Path(path).read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise OverlayPlacementError(f"overlay {path} is not a PNG file")
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3 or img.shape[2] != 4:
        raise OverlayPlacementError(f"overlay {path} has no alpha channel (needs an RGBA PNG)")
    alpha = img[:, :, 3]
    if alpha.dtype != np.uint8:
        alpha = (alpha // 257).astype(np.uint8)
    ys, xs = np.nonzero(alpha > OPAQUE_ALPHA)
    if len(xs) == 0:
        raise OverlayPlacementError(f"overlay {path} is fully transparent")
    h, w = alpha.shape
    opaque = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    return ImageInfo(width=int(w), height=int(h), opaque=opaque, sha256=hashlib.sha256(data).hexdigest())


def place_overlay(cfg: CampaignOverlay, image: ImageInfo, path: Path) -> OverlayPlacement:
    """Pure: scale and centre the image as configured. Even pixel sizes (yuv420p)."""
    width = max(2, round(FRAME_W * cfg.scale / 2) * 2)
    height = max(2, round(image.height * width / image.width / 2) * 2)
    cx = cfg.position.x_pct / 100 * FRAME_W
    cy = cfg.position.y_pct / 100 * FRAME_H
    x0, y0 = round(cx - width / 2), round(cy - height / 2)
    sx, sy = width / image.width, height / image.height
    ox0, oy0, ox1, oy1 = image.opaque
    # Floor the start, ceil the end: never smaller than the real visible area.
    opaque = Rect(
        x0=x0 + math.floor(ox0 * sx),
        y0=y0 + math.floor(oy0 * sy),
        x1=x0 + math.ceil(ox1 * sx),
        y1=y0 + math.ceil(oy1 * sy),
    )
    return OverlayPlacement(
        path=path,
        rect=Rect(x0=x0, y0=y0, x1=x0 + width, y1=y0 + height),
        opaque_rect=opaque,
        width=width,
        height=height,
        sha256=image.sha256,
    )


def _grow(r: Rect, by: int) -> Rect:
    return Rect(x0=r.x0 - by, y0=r.y0 - by, x1=r.x1 + by, y1=r.y1 + by)


def caption_band(styles: Iterable[Style]) -> Rect:
    """Everywhere a caption line can be: full width, the 62–70 % safe zone ± the largest caption half-height."""
    half = max((s.size * 0.65 + max(s.outline or 0, 8 if s.box else 4) for s in styles), default=60)
    lo, hi = SAFE_ZONE_PCT
    return Rect(x0=0, y0=round(lo / 100 * FRAME_H - half), x1=FRAME_W, y1=round(hi / 100 * FRAME_H + half))


def hook_band(styles: Iterable[Style]) -> Rect:
    """Everywhere the hook can be: full width, HOOK_MAX_LINES lines of the largest hook size around its centre."""
    size = max((round(s.size * 1.1) for s in styles), default=86)
    half = HOOK_MAX_LINES * size * 1.25 / 2 + 18  # line height ~1.25 x size, plus the box padding (outline 18)
    cy = HOOK_Y_PCT / 100 * FRAME_H
    return Rect(x0=0, y0=round(cy - half), x1=FRAME_W, y1=round(cy + half))


def ui_zones() -> dict[str, Rect]:
    right_x = round(FRAME_W * (1 - UI_RIGHT_PCT / 100))
    bottom_y = round(FRAME_H * (1 - UI_BOTTOM_PCT / 100))
    return {
        "platform UI (top 150 px)": Rect(x0=0, y0=0, x1=FRAME_W, y1=UI_TOP_PX),
        "platform UI (right 15 %)": Rect(x0=right_x, y0=0, x1=FRAME_W, y1=FRAME_H),
        "platform UI (bottom 20 %)": Rect(x0=0, y0=bottom_y, x1=FRAME_W, y1=FRAME_H),
    }


def clearance_problems(placement: OverlayPlacement, styles: Iterable[Style]) -> list[str]:
    """Pure: every reason the overlay's visible pixels would cover something they must not. Empty = clear."""
    styles = list(styles)
    o = placement.opaque_rect
    problems: list[str] = []
    if o.x0 < 0 or o.y0 < 0 or o.x1 > FRAME_W or o.y1 > FRAME_H:
        problems.append(f"overlay visible area {_fmt(o)} runs outside the {FRAME_W}x{FRAME_H} frame")
    keep_out = {"captions": caption_band(styles), "hook": hook_band(styles), **ui_zones()}
    for name, zone in keep_out.items():
        if _grow(o, MARGIN_PX).intersects(zone):
            problems.append(f"overlay visible area {_fmt(o)} would cover the {name} {_fmt(zone)}")
    return problems


def ad_label_rect(alignment: int, x: int, y: int, text: str) -> Rect:
    """Estimated box of the "Ad" label drawn with `\\an<alignment>\\pos(x,y)` (top-anchored alignments 7/8/9)."""
    w = round(len(text) * AD_LABEL_SIZE * AD_LABEL_CHAR_W) + 2 * AD_LABEL_PAD
    h = round(AD_LABEL_SIZE * 1.25) + 2 * AD_LABEL_PAD
    left = {7: x, 8: x - w // 2, 9: x - w}[alignment]
    return Rect(x0=left - AD_LABEL_PAD, y0=y - AD_LABEL_PAD, x1=left - AD_LABEL_PAD + w, y1=y - AD_LABEL_PAD + h)


def place_ad_label(text: str, overlay: OverlayPlacement | None) -> AdLabelPlacement:
    """The first label spot whose box is clear of the overlay's visible area (default top-left when no overlay)."""
    spots = [(a, x, y, ad_label_rect(a, x, y, text)) for a, x, y in AD_LABEL_SPOTS]
    for alignment, x, y, rect in spots:
        if overlay is None or not _grow(rect, MARGIN_PX).intersects(overlay.opaque_rect):
            return AdLabelPlacement(alignment=alignment, x=x, y=y, rect=rect)
    assert overlay is not None
    raise OverlayPlacementError(f"no place for the 'Ad' label clear of the overlay {_fmt(overlay.opaque_rect)}")


def resolve(cfg: CampaignOverlay, root: Path, styles: Iterable[Style], ad_text: str) -> OverlayPlacement:
    """Load, place and check a campaign overlay; raise OverlayPlacementError with every problem found."""
    path = (root / cfg.path).resolve()
    assets = (root / "campaigns" / "assets").resolve()
    if not path.is_relative_to(assets):
        raise OverlayPlacementError(f"overlay {cfg.path} is outside campaigns/assets/")
    if not path.is_file():
        raise OverlayPlacementError(f"overlay file {cfg.path} not found (campaigns/assets/ is not committed)")
    placement = place_overlay(cfg, read_image(path), path)
    problems = clearance_problems(placement, styles)
    if problems:
        raise OverlayPlacementError("; ".join(problems))
    place_ad_label(ad_text, placement)  # raises if the label has nowhere to go
    return placement


def _fmt(r: Rect) -> str:
    return f"x {r.x0}-{r.x1}, y {r.y0}-{r.y1}"
