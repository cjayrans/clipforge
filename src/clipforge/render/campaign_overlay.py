"""Campaign overlay image: placement, clearance checks and the "Ad" label position (COMPLIANCE invariant 3).

A campaign may supply an image (PNG with alpha, e.g. a streamer's channel banner) that must be visible on every
clip (`schemas.CampaignOverlay`). Generic for every campaign; nothing here knows about any one campaign.

Geometry is in the 1080x1920 output frame. "Opaque rect" = the bounding box of the image's visible pixels
(alpha > OPAQUE_ALPHA) after scaling; transparent margins may overlap anything.

Rules (each one raises `OverlayPlacementError`; nothing here ever shrinks or moves the overlay on its own):
- the opaque rect lies inside the frame and outside the platform UI zones (top 150 px, right 15 %, bottom 20 %);
- it misses the caption band (the 62–70 % safe zone, widened by the largest caption box) and the hook box;
- the "Ad" label is drawn at the first spot clear of the hook, captions, UI zones and the opaque rect (default: left
  edge just under the hook). If no spot is clear, the render is refused.
The overlay is composited before the `ass` filter, so the label, hook and captions are drawn on top of it anyway.
"""

from __future__ import annotations

import functools
import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from clipforge import compliance
from clipforge.config import Style
from clipforge.render.captions import SAFE_ZONE_PCT
from clipforge.render.overlays import AD_LABEL_PAD, AD_LABEL_POS, AD_LABEL_SIZE, HOOK_MAX_LINES, HOOK_Y_PCT
from clipforge.schemas import CampaignOverlay, Rect

FRAME_W, FRAME_H = 1080, 1920
OPAQUE_ALPHA = 16  # alpha above this counts as visible

# Platform UI zones (single source: compliance.ui_zones). Captions are kept out of them by `captions.SAFE_ZONE_PCT`.
UI_TOP_PX = compliance.UI_TOP_PX
UI_RIGHT_PCT = compliance.UI_RIGHT_PCT
UI_BOTTOM_PCT = compliance.UI_BOTTOM_PCT

# "Ad" label box estimate (AdLabel style: size 48, BorderStyle 3 with the outline width as padding).
AD_LABEL_CHAR_W = 0.75  # generous average glyph width per point of font size
MARGIN_PX = 8  # gap kept between the overlay's visible pixels / the label and anything they must not touch


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


def hook_half_height(styles: Iterable[Style]) -> float:
    size = max((round(s.size * 1.1) for s in styles), default=86)
    return HOOK_MAX_LINES * size * 1.25 / 2 + 18  # line height ~1.25 x size, plus the box padding (outline 18)


DEFAULT_HOOK_CY = round(HOOK_Y_PCT / 100 * FRAME_H)


def hook_band(styles: Iterable[Style], cy: float | None = None) -> Rect:
    """Everywhere the hook can be: full width, HOOK_MAX_LINES lines of the largest hook size around its centre
    (default: HOOK_Y_PCT of the height)."""
    half = hook_half_height(styles)
    cy = DEFAULT_HOOK_CY if cy is None else cy
    return Rect(x0=0, y0=round(cy - half), x1=FRAME_W, y1=round(cy + half))


def ui_zones() -> dict[str, Rect]:
    return compliance.ui_zones()


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
    """Estimated box of the "Ad" label drawn with `\\an<alignment>\\pos(x,y)` (top-anchored alignments 7/8/9),
    padding included."""
    w = round(len(text) * AD_LABEL_SIZE * AD_LABEL_CHAR_W) + 2 * AD_LABEL_PAD
    h = round(AD_LABEL_SIZE * 1.25) + 2 * AD_LABEL_PAD
    left = {7: x, 8: x - w // 2, 9: x - w}[alignment]
    return Rect(x0=left - AD_LABEL_PAD, y0=y - AD_LABEL_PAD, x1=left - AD_LABEL_PAD + w, y1=y - AD_LABEL_PAD + h)


def ad_label_spots(
    styles: Iterable[Style],
    overlay: OverlayPlacement | None,
    hook: Rect | None = None,
    panel: Rect | None = None,
) -> list[tuple[int, int, int]]:
    """Candidate label spots, in order.

    Default layouts (no facecam panel, hook at its default spot): AD_LABEL_POS (left edge under the hook), the left
    edge just under the hook's worst case for these styles (if a bigger style pushes it lower), the left edge just
    under the overlay's visible area. With a facecam panel (stacked, label layout 3): first the panel's top-left
    corner just under the top platform UI zone, then under the hook, then under the overlay."""
    styles = list(styles)
    x = AD_LABEL_POS[0]
    hook = hook if hook is not None else hook_band(styles)
    under_hook = hook.y1 + MARGIN_PX + AD_LABEL_PAD
    spots: list[tuple[int, int, int]] = []
    if panel is not None:
        spots.append((7, x, max(panel.y0, UI_TOP_PX) + MARGIN_PX + AD_LABEL_PAD))
        spots.append((7, x, under_hook))
    else:
        spots += [(7, x, AD_LABEL_POS[1]), (7, x, max(AD_LABEL_POS[1], under_hook))]
    if overlay is not None:
        spots.append((7, x, max(under_hook, overlay.opaque_rect.y1 + MARGIN_PX + AD_LABEL_PAD)))
    return list(dict.fromkeys(spots))


def ad_label_problems(
    rect: Rect,
    styles: Iterable[Style],
    overlay: OverlayPlacement | None,
    hook: Rect | None = None,
    face: Rect | None = None,
) -> list[str]:
    """Pure: every reason a label box at `rect` would be hidden or would cover something. Empty = clear."""
    styles = list(styles)
    problems: list[str] = []
    if rect.x0 < 0 or rect.y0 < 0 or rect.x1 > FRAME_W or rect.y1 > FRAME_H:
        problems.append("outside the frame")
    keep_out = {"hook": hook if hook is not None else hook_band(styles), "captions": caption_band(styles), **ui_zones()}
    if overlay is not None:
        keep_out["campaign overlay"] = overlay.opaque_rect
    if face is not None:
        keep_out["streamer's face"] = face
    grown = _grow(rect, MARGIN_PX)
    problems += [f"covers the {name} {_fmt(zone)}" for name, zone in keep_out.items() if grown.intersects(zone)]
    return problems


def place_ad_label(
    text: str,
    overlay: OverlayPlacement | None,
    styles: Iterable[Style],
    hook: Rect | None = None,
    face: Rect | None = None,
    panel: Rect | None = None,
) -> AdLabelPlacement:
    """The first spot (see `ad_label_spots`) whose label box misses the hook, the captions, the platform UI zones,
    the overlay's visible area and the face. Raises if none does: the label is never drawn where it could be hidden."""
    styles = list(styles)
    tried: list[str] = []
    for alignment, x, y in ad_label_spots(styles, overlay, hook, panel):
        rect = ad_label_rect(alignment, x, y, text)
        problems = ad_label_problems(rect, styles, overlay, hook, face)
        if not problems:
            return AdLabelPlacement(alignment=alignment, x=x, y=y, rect=rect)
        tried.append(f"({x}, {y}): " + ", ".join(problems))
    raise OverlayPlacementError("no clear place for the 'Ad' label: " + "; ".join(tried))


@dataclass(frozen=True)
class TextPlacement:
    """Where the hook and the "Ad" label go for one layout (output px)."""

    hook_cy: int
    hook_rect: Rect
    ad: AdLabelPlacement
    caption_rect: Rect


def hook_spots(styles: Iterable[Style], overlay: OverlayPlacement | None, panel: Rect | None) -> list[int]:
    """Hook centres to try. Default layouts keep the hook at its default spot only. With a facecam panel (stacked),
    also just under the overlay's visible area and just under the panel (the hook must never cover the face)."""
    styles = list(styles)
    half = hook_half_height(styles)
    spots = [DEFAULT_HOOK_CY]
    if panel is not None:
        if overlay is not None:
            spots.append(round(overlay.opaque_rect.y1 + MARGIN_PX + half))
        spots.append(round(panel.y1 + MARGIN_PX + half))
    return list(dict.fromkeys(spots))


def hook_problems(
    band: Rect, styles: Iterable[Style], overlay: OverlayPlacement | None, face: Rect | None
) -> list[str]:
    """Pure: every reason the hook's box at `band` would cover something or be covered."""
    styles = list(styles)
    problems: list[str] = []
    if band.y0 < 0 or band.y1 > FRAME_H:
        problems.append("outside the frame")
    zones = ui_zones()
    keep_out = {
        "captions": caption_band(styles),
        **{k: v for k, v in zones.items() if "right" not in k},  # the hook is centred and wrapped; full-width band
    }
    if overlay is not None:
        keep_out["campaign overlay"] = overlay.opaque_rect
    if face is not None:
        keep_out["streamer's face"] = face
    grown = _grow(band, MARGIN_PX)
    problems += [f"covers the {name} {_fmt(zone)}" for name, zone in keep_out.items() if grown.intersects(zone)]
    return problems


def overlay_base_problems(placement: OverlayPlacement, styles: Iterable[Style], face: Rect | None) -> list[str]:
    """Pure: the overlay's visible pixels against everything except the hook (which is placed after it)."""
    styles = list(styles)
    o = placement.opaque_rect
    problems: list[str] = []
    if o.x0 < 0 or o.y0 < 0 or o.x1 > FRAME_W or o.y1 > FRAME_H:
        problems.append(f"overlay visible area {_fmt(o)} runs outside the {FRAME_W}x{FRAME_H} frame")
    keep_out = {"captions": caption_band(styles), **ui_zones()}
    if face is not None:
        keep_out["streamer's face"] = face
    for name, zone in keep_out.items():
        if _grow(o, MARGIN_PX).intersects(zone):
            problems.append(f"overlay visible area {_fmt(o)} would cover the {name} {_fmt(zone)}")
    return problems


def place_texts(
    ad_text: str,
    overlay: OverlayPlacement | None,
    styles: Iterable[Style],
    *,
    face: Rect | None = None,
    panel: Rect | None = None,
) -> TextPlacement:
    """Hook and "Ad" label for one layout, every clearance checked (overlay vs captions/UI/face/frame, hook vs
    captions/top and bottom UI/overlay/face, label vs hook/captions/UI/overlay/face). Raises OverlayPlacementError
    with every reason when no arrangement works; nothing is shrunk."""
    styles = list(styles)
    if overlay is not None:
        problems = overlay_base_problems(overlay, styles, face)
        if problems:
            raise OverlayPlacementError("; ".join(problems))
    tried: list[str] = []
    for cy in hook_spots(styles, overlay, panel):
        band = hook_band(styles, cy)
        problems = hook_problems(band, styles, overlay, face)
        if not problems:
            break
        tried.append(f"hook at y {band.y0}-{band.y1}: " + ", ".join(problems))
    else:
        raise OverlayPlacementError("no clear place for the hook: " + "; ".join(tried))
    ad = place_ad_label(ad_text, overlay, styles, hook=band, face=face, panel=panel)
    return TextPlacement(hook_cy=cy, hook_rect=band, ad=ad, caption_rect=caption_band(styles))


def load_placement(cfg: CampaignOverlay, root: Path) -> OverlayPlacement:
    """Load and place a campaign overlay (path rules, PNG with alpha); no clearance checks."""
    path = (root / cfg.path).resolve()
    assets = (root / "campaigns" / "assets").resolve()
    if not path.is_relative_to(assets):
        raise OverlayPlacementError(f"overlay {cfg.path} is outside campaigns/assets/")
    if not path.is_file():
        raise OverlayPlacementError(f"overlay file {cfg.path} not found (campaigns/assets/ is not committed)")
    return place_overlay(cfg, read_image(path), path)


def resolve(cfg: CampaignOverlay, root: Path, styles: Iterable[Style], ad_text: str) -> OverlayPlacement:
    """Load, place and check a campaign overlay against the default (fit/crop) layout; raise OverlayPlacementError
    with every problem found."""
    styles = list(styles)
    placement = load_placement(cfg, root)
    problems = clearance_problems(placement, styles)
    if problems:
        raise OverlayPlacementError("; ".join(problems))
    place_ad_label(ad_text, placement, styles)  # raises if the label has nowhere to go
    return placement


def _fmt(r: Rect) -> str:
    return f"x {r.x0}-{r.x1}, y {r.y0}-{r.y1}"
