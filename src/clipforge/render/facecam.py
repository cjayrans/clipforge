"""Facecam detection: where the streamer's webcam sits in a stream source (layouts, feat/reframe).

Once per source (the result is stored on `sources.facecam_json`, "none" included):
1. Sample `SAMPLE_FRAMES` frames spread over the source (skipping the first and last 5 %: intros, end screens).
2. Detect faces on overlapping tiles. BlazeFace short-range (the bundled MediaPipe model) scales its input to
   128x128, so a facecam face of ~100 px in a 1080p frame would vanish if the whole frame went in; on a tile of
   `TILE_FRAC` x the frame height it stays large enough. Boxes are mapped back to the frame and de-duplicated (NMS).
3. Pure core `pick_facecam`: drop faces taller than `MAX_FACE_H_FRAC` of the frame (full-screen camera scenes) or
   below `MIN_SCORE`, cluster the rest by centre, and keep the cluster seen in the most frames. It must be seen in at
   least `MIN_HIT_RATIO` of the sampled frames (in-game faces move around and do not cluster). Face box = the
   per-coordinate median of that cluster; facecam box = the face padded to the 1080x640 panel aspect
   (`pad_face`), kept inside the frame.

Per clip, `face_presence` crops that box out of the clip's own range at `PRESENCE_FPS` and checks that a face is
really there (the streamer may have switched scenes); the stacked layout needs `layout.STACKED_MIN_FACE_RATIO`.

A campaign can skip detection with a manual `facecam_box` (percent of the source frame; `pct_to_px`).
Frames come from ffmpeg (raw RGB on stdout); the readers are injectable so the logic is tested without media.
"""

from __future__ import annotations

import logging
import math
import statistics
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from clipforge.render.reframe import FaceBox
from clipforge.schemas import FacecamBoxPct, PxBox

log = logging.getLogger(__name__)

DETECTOR_VERSION = "facecam-v1"  # stored with each result; a new version re-detects
SAMPLE_FRAMES = 40
SKIP_EDGE_FRAC = 0.05
TILE_FRAC = 0.4  # tile side as a fraction of the frame height (432 px on 1080p), 50 % overlap
MIN_SCORE = 0.5
MAX_FACE_H_FRAC = 0.35
CLUSTER_TOL_FRAC = 0.04  # of the frame width, between face centres
MIN_HIT_RATIO = 0.4
MIN_HITS = 3
NMS_IOU = 0.3
PANEL_ASPECT = 1080 / 640  # the stacked layout's facecam panel
FACE_TO_BOX_H = 2.4  # facecam box height = 2.4 x face height
FACE_CENTRE_Y = 0.45  # face centre sits 45 % down the box
PRESENCE_FPS = 2.0
PRESENCE_MIN_FACE_H = 0.12  # a face in the facecam crop is at least 12 % of its height
GRAB_TIMEOUT_S = 120
MAX_GRAB_FAILURES = 3

ImageDetector = Callable[[Any], Sequence[FaceBox]]  # RGB HxWx3 uint8 array -> faces in its pixels
FrameGrabber = Callable[[Path, float, int, int], Any]  # (video, t, w, h) -> RGB array or None
CropGrabber = Callable[[Path, PxBox, float, float], list[Any]]  # (video, box, start, end) -> RGB crops


@dataclass(frozen=True)
class FacecamDetection:
    """Result of `detect_facecam` for one source (stored as JSON on `sources.facecam_json`)."""

    status: Literal["found", "none"]
    src_w: int
    src_h: int
    hits: int
    samples: int
    box: PxBox | None = None  # facecam box (source px), the part drawn in the stacked layout's top panel
    face: PxBox | None = None  # median face box (source px)
    version: str = DETECTOR_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "src_w": self.src_w,
            "src_h": self.src_h,
            "hits": self.hits,
            "samples": self.samples,
            "box": self.box.model_dump() if self.box else None,
            "face": self.face.model_dump() if self.face else None,
            "version": self.version,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> FacecamDetection:
        return cls(
            status="found" if data.get("status") == "found" else "none",
            src_w=int(data.get("src_w", 0)),
            src_h=int(data.get("src_h", 0)),
            hits=int(data.get("hits", 0)),
            samples=int(data.get("samples", 0)),
            box=PxBox.model_validate(data["box"]) if data.get("box") else None,
            face=PxBox.model_validate(data["face"]) if data.get("face") else None,
            version=str(data.get("version", "")),
        )

    def summary(self) -> str:
        if self.status != "found" or self.box is None:
            return f"no facecam found (a stable face in {self.hits} of {self.samples} sampled frames)"
        b = self.box
        return (
            f"facecam x {b.x}, y {b.y}, {b.w}x{b.h} px of {self.src_w}x{self.src_h} "
            f"(face in {self.hits} of {self.samples} sampled frames)"
        )


@dataclass(frozen=True)
class FacecamSpec:
    """The facecam a render uses: a detected box (source px) or the campaign's manual box (percent)."""

    origin: Literal["detected", "campaign"]
    box: PxBox | None = None
    box_pct: FacecamBoxPct | None = None

    def resolve(self, src_w: int, src_h: int) -> PxBox:
        if self.box_pct is not None:
            return pct_to_px(self.box_pct, src_w, src_h)
        if self.box is None:
            raise ValueError("facecam spec has no box")
        return clamp_box(self.box, src_w, src_h)


@dataclass(frozen=True)
class Presence:
    """How often a face shows in the facecam box over a clip; `face` = its median box in crop pixels."""

    ratio: float
    samples: int
    face: PxBox | None = None
    problem: str = ""


# --------------------------------------------------------------------------------------------------
# Pure geometry
# --------------------------------------------------------------------------------------------------


def _even(n: float) -> int:
    return max(2, int(n // 2) * 2)


def clamp_box(box: PxBox, src_w: int, src_h: int) -> PxBox:
    """Even-sized box shifted (never grown) to lie inside the frame."""
    w = min(_even(box.w), src_w - src_w % 2)
    h = min(_even(box.h), src_h - src_h % 2)
    x = min(max(0, box.x), src_w - w)
    y = min(max(0, box.y), src_h - h)
    return PxBox(x=x, y=y, w=w, h=h)


def fit_aspect(box: PxBox, aspect: float = PANEL_ASPECT) -> PxBox:
    """The largest box of `aspect` (w/h) centred inside `box`, even-sized."""
    if box.w / box.h > aspect:
        h = _even(box.h)
        w = min(_even(h * aspect), _even(box.w))
    else:
        w = _even(box.w)
        h = min(_even(w / aspect), _even(box.h))
    return PxBox(x=box.x + (box.w - w) // 2, y=box.y + (box.h - h) // 2, w=w, h=h)


def pct_to_px(pct: FacecamBoxPct, src_w: int, src_h: int) -> PxBox:
    """A campaign's manual facecam box in source pixels, trimmed to the panel aspect around its centre."""
    raw = PxBox(
        x=max(0, round(pct.x_pct / 100 * src_w)),
        y=max(0, round(pct.y_pct / 100 * src_h)),
        w=max(2, round(pct.w_pct / 100 * src_w)),
        h=max(2, round(pct.h_pct / 100 * src_h)),
    )
    return clamp_box(fit_aspect(clamp_box(raw, src_w, src_h)), src_w, src_h)


def pad_face(face: PxBox, src_w: int, src_h: int) -> PxBox:
    """Facecam box around a face: panel aspect, `FACE_TO_BOX_H` x the face height, face centre `FACE_CENTRE_Y` down,
    shrunk to fit the frame if needed and shifted inside it."""
    h = face.h * FACE_TO_BOX_H
    w = h * PANEL_ASPECT
    if w > src_w:
        w, h = src_w, src_w / PANEL_ASPECT
    if h > src_h:
        h, w = src_h, src_h * PANEL_ASPECT
    w_i, h_i = _even(w), _even(h)
    cx, cy = face.x + face.w / 2, face.y + face.h / 2
    x = round(cx - w_i / 2)
    y = round(cy - FACE_CENTRE_Y * h_i)
    return clamp_box(PxBox(x=max(0, x), y=max(0, y), w=w_i, h=h_i), src_w, src_h)


def tiles(src_w: int, src_h: int) -> list[tuple[int, int, int]]:
    """(x, y, side) of overlapping square tiles covering the frame."""
    side = max(64, min(src_w, src_h, round(src_h * TILE_FRAC)))
    stride = max(1, side // 2)

    def starts(length: int) -> list[int]:
        out = list(range(0, max(1, length - side + 1), stride))
        if out[-1] + side < length:
            out.append(length - side)
        return out

    return [(x, y, side) for y in starts(src_h) for x in starts(src_w)]


def _iou(a: FaceBox, b: FaceBox) -> float:
    x0, y0 = max(a.x, b.x), max(a.y, b.y)
    x1, y1 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = a.area + b.area - inter
    return inter / union if union > 0 else 0.0


def nms(boxes: Sequence[FaceBox], iou: float = NMS_IOU) -> list[FaceBox]:
    kept: list[FaceBox] = []
    for box in sorted(boxes, key=lambda b: b.score, reverse=True):
        if all(_iou(box, k) <= iou for k in kept):
            kept.append(box)
    return kept


def pick_facecam(frames: Sequence[Sequence[FaceBox]], src_w: int, src_h: int) -> FacecamDetection:
    """Pure: the stable facecam among per-frame face detections (frame pixels), or status "none"."""
    n = len(frames)
    tol = CLUSTER_TOL_FRAC * src_w
    clusters: list[dict[int, FaceBox]] = []  # frame index -> best face of that cluster in that frame
    for i, faces in enumerate(frames):
        for f in faces:
            if f.score < MIN_SCORE or f.h > MAX_FACE_H_FRAC * src_h or f.w <= 0 or f.h <= 0:
                continue
            cx, cy = f.x + f.w / 2, f.y + f.h / 2
            for cluster in clusters:
                mx = statistics.median(c.x + c.w / 2 for c in cluster.values())
                my = statistics.median(c.y + c.h / 2 for c in cluster.values())
                if abs(cx - mx) <= tol and abs(cy - my) <= tol:
                    if i not in cluster or cluster[i].score < f.score:
                        cluster[i] = f
                    break
            else:
                clusters.append({i: f})
    best = max(clusters, key=len, default={})
    hits = len(best)
    if hits < max(MIN_HITS, math.ceil(MIN_HIT_RATIO * n)):
        return FacecamDetection(status="none", src_w=src_w, src_h=src_h, hits=hits, samples=n)
    members = list(best.values())
    face = PxBox(
        x=max(0, round(statistics.median(f.x for f in members))),
        y=max(0, round(statistics.median(f.y for f in members))),
        w=max(1, round(statistics.median(f.w for f in members))),
        h=max(1, round(statistics.median(f.h for f in members))),
    )
    return FacecamDetection(
        status="found",
        src_w=src_w,
        src_h=src_h,
        hits=hits,
        samples=n,
        box=pad_face(face, src_w, src_h),
        face=face,
    )


def presence_from_faces(frames: Sequence[Sequence[FaceBox]], crop_w: int, crop_h: int) -> Presence:
    """Pure: share of facecam crops with a real face (score, size), and the median face box (crop pixels)."""
    found: list[FaceBox] = []
    for faces in frames:
        good = [f for f in faces if f.score >= MIN_SCORE and f.h >= PRESENCE_MIN_FACE_H * crop_h]
        if good:
            found.append(max(good, key=lambda f: f.area))
    if not frames:
        return Presence(ratio=0.0, samples=0, problem="no frames read from the facecam box")
    face = None
    if found:
        x0 = max(0.0, statistics.median(f.x for f in found))
        y0 = max(0.0, statistics.median(f.y for f in found))
        face = PxBox(
            x=round(x0),
            y=round(y0),
            w=max(1, min(round(statistics.median(f.w for f in found)), crop_w - round(x0))),
            h=max(1, min(round(statistics.median(f.h for f in found)), crop_h - round(y0))),
        )
    return Presence(ratio=len(found) / len(frames), samples=len(frames), face=face)


def sample_times(duration_s: float, n: int = SAMPLE_FRAMES) -> list[float]:
    lo, hi = duration_s * SKIP_EDGE_FRAC, duration_s * (1 - SKIP_EDGE_FRAC)
    if n <= 1 or hi <= lo:
        return [duration_s / 2]
    return [round(lo + (hi - lo) * i / (n - 1), 3) for i in range(n)]


def detect_tiled(rgb: Any, detector: ImageDetector, src_w: int, src_h: int) -> list[FaceBox]:
    """Faces in one full frame (frame pixels), found tile by tile."""
    faces: list[FaceBox] = []
    for x, y, side in tiles(src_w, src_h):
        for f in detector(rgb[y : y + side, x : x + side]):
            faces.append(FaceBox(x=f.x + x, y=f.y + y, w=f.w, h=f.h, score=f.score))
    return nms(faces)


# --------------------------------------------------------------------------------------------------
# Frame readers (ffmpeg, raw RGB on stdout)
# --------------------------------------------------------------------------------------------------


def _ffmpeg_rgb(args: list[str]) -> bytes:
    try:
        done = subprocess.run(args, check=True, capture_output=True, timeout=GRAB_TIMEOUT_S)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        raise OSError(f"ffmpeg frame read failed: {type(exc).__name__}") from exc
    return done.stdout


def grab_frame(path: Path, t: float, w: int, h: int, crop: PxBox | None = None) -> Any:
    """One RGB frame at `t` seconds (fast input seek), optionally cropped. None if it cannot be read."""
    import numpy as np

    vf = ["-vf", f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y}"] if crop is not None else []
    out_w, out_h = (crop.w, crop.h) if crop is not None else (w, h)
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
        "-an", "-sn", "-dn", *vf, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]  # fmt: skip
    try:
        data = _ffmpeg_rgb(args)
    except OSError:
        return None
    if len(data) < out_w * out_h * 3:
        return None
    return np.frombuffer(data[: out_w * out_h * 3], dtype=np.uint8).reshape(out_h, out_w, 3)


def grab_crops(path: Path, box: PxBox, start_s: float, end_s: float, fps: float = PRESENCE_FPS) -> list[Any]:
    """The facecam box cropped out of [start_s, end_s] at `fps` (one ffmpeg run)."""
    import numpy as np

    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
        "-ss", f"{start_s:.3f}", "-t", f"{max(0.1, end_s - start_s):.3f}", "-i", str(path),
        "-an", "-sn", "-dn", "-vf", f"fps={fps:g},crop={box.w}:{box.h}:{box.x}:{box.y}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]  # fmt: skip
    data = _ffmpeg_rgb(args)
    size = box.w * box.h * 3
    return [
        np.frombuffer(data[i : i + size], dtype=np.uint8).reshape(box.h, box.w, 3)
        for i in range(0, len(data) - size + 1, size)
    ]


# --------------------------------------------------------------------------------------------------
# Detection runs
# --------------------------------------------------------------------------------------------------


def detect_facecam(
    path: Path,
    src_w: int,
    src_h: int,
    duration_s: float,
    *,
    detector: ImageDetector,
    grab: FrameGrabber = grab_frame,
    n: int = SAMPLE_FRAMES,
) -> FacecamDetection | None:
    """Sample the source and pick its facecam. None when too few frames could be read (nothing to store)."""
    frames: list[list[FaceBox]] = []
    failures = 0
    for t in sample_times(duration_s, n):
        rgb = grab(path, t, src_w, src_h)
        if rgb is None:
            failures += 1
            if failures >= MAX_GRAB_FAILURES and not frames:
                break
            continue
        frames.append(detect_tiled(rgb, detector, src_w, src_h))
    if len(frames) < max(MIN_HITS, n // 2):
        log.warning("facecam detection read only %d of %d frames of %s", len(frames), n, path.name)
        return None
    return pick_facecam(frames, src_w, src_h)


def face_presence(
    path: Path,
    box: PxBox,
    start_s: float,
    end_s: float,
    *,
    detector: ImageDetector,
    grab: CropGrabber = grab_crops,
) -> Presence:
    try:
        crops = grab(path, box, start_s, end_s)
    except OSError as exc:
        return Presence(ratio=0.0, samples=0, problem=str(exc))
    return presence_from_faces([list(detector(c)) for c in crops], box.w, box.h)


def default_detect(path: Path) -> FacecamDetection | None:
    """Facecam detection with the bundled MediaPipe model. None if mediapipe or the media is unavailable."""
    from clipforge.qa.probe import ProbeError, probe

    try:
        from clipforge.render.face_detect import MediaPipeImageDetector
    except ImportError:
        log.warning("mediapipe not installed; facecam detection skipped")
        return None
    try:
        src = probe(path)
    except (ProbeError, OSError) as exc:
        log.warning("facecam detection: cannot probe %s (%s)", path.name, exc)
        return None
    if not src.width or not src.height or src.duration_s <= 0:
        return None
    try:
        detector = MediaPipeImageDetector()
    except ImportError:
        log.warning("mediapipe not installed; facecam detection skipped")
        return None
    with detector:
        return detect_facecam(path, src.width, src.height, src.duration_s, detector=detector)


def default_presence(path: Path, box: PxBox, start_s: float, end_s: float) -> Presence:
    try:
        from clipforge.render.face_detect import MediaPipeImageDetector

        detector = MediaPipeImageDetector()
    except ImportError:
        return Presence(ratio=0.0, samples=0, problem="no face detector (mediapipe not installed)")
    with detector:
        return face_presence(path, box, start_s, end_s, detector=detector)


def draw_debug(rgb: Any, detection: FacecamDetection | None, box: PxBox | None, out: Path) -> None:
    """Write a JPEG of `rgb` with the face (green), facecam (red) and centre stacked-gameplay window (blue) drawn."""
    import cv2

    img = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = img.shape[:2]
    if detection is not None and detection.face is not None:
        f = detection.face
        cv2.rectangle(img, (f.x, f.y), (f.x + f.w, f.y + f.h), (0, 255, 0), 3)
    if box is not None:
        cv2.rectangle(img, (box.x, box.y), (box.x + box.w, box.y + box.h), (0, 0, 255), 4)
    gw = min(w, _even(h * 1080 / 1280))
    gx = (w - gw) // 2
    cv2.rectangle(img, (gx, 0), (gx + gw, h - 1), (255, 0, 0), 2)
    if not cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 85]):
        raise OSError(f"could not write {out}")


def draw_debug_stacked(rgb: Any, box: PxBox, out: Path) -> None:
    """Write a half-size JPEG mock of the stacked layout (facecam box on top, centre gameplay crop below)."""
    import cv2
    import numpy as np

    h, w = rgb.shape[:2]
    top = cv2.resize(np.ascontiguousarray(rgb[box.y : box.y + box.h, box.x : box.x + box.w]), (540, 320))
    gw = min(w, _even(h * 1080 / 1280))
    gx = (w - gw) // 2
    bottom = cv2.resize(np.ascontiguousarray(rgb[:, gx : gx + gw]), (540, 640))
    img = cv2.cvtColor(np.vstack([top, bottom]), cv2.COLOR_RGB2BGR)
    if not cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 85]):
        raise OSError(f"could not write {out}")
