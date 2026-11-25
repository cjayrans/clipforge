"""A fake campaign overlay image for tests: an RGBA PNG with transparent margins around an opaque box."""

from __future__ import annotations

from pathlib import Path

FAKE_OVERLAY_REL = "campaigns/assets/fake-campaign/banner.png"


def write_overlay_png(
    root: Path,
    rel: str = FAKE_OVERLAY_REL,
    *,
    size: tuple[int, int] = (800, 200),
    margin: tuple[int, int] = (40, 30),
    alpha: bool = True,
) -> Path:
    """Write a `size` (w, h) PNG under `root/rel`: fully transparent margins of `margin` (x, y) px and an opaque
    magenta box inside. `alpha=False` writes a plain RGB PNG (no alpha channel)."""
    import cv2
    import numpy as np

    w, h = size
    mx, my = margin
    img = np.zeros((h, w, 4 if alpha else 3), dtype=np.uint8)
    img[my : h - my, mx : w - mx, 0] = 255  # blue
    img[my : h - my, mx : w - mx, 2] = 255  # red -> magenta
    if alpha:
        img[my : h - my, mx : w - mx, 3] = 255
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    path.write_bytes(buf.tobytes())
    return path
