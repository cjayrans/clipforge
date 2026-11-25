"""Facecam detection (render/facecam.py): tiling, clustering, padding, presence, storage format."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from clipforge.render import facecam as fc
from clipforge.render.reframe import FaceBox
from clipforge.schemas import FacecamBoxPct, PxBox

W, H = 1920, 1080
CAM_FACE = FaceBox(x=1580, y=760, w=100, h=110, score=0.9)  # a facecam in the bottom-right corner


def _jitter(f: FaceBox, rng: random.Random, px: int = 3) -> FaceBox:
    return FaceBox(
        x=f.x + rng.randint(-px, px), y=f.y + rng.randint(-px, px), w=f.w + rng.randint(-2, 2), h=f.h, score=f.score
    )


def _frames(hit_frames: int, total: int = 40, seed: int = 1) -> list[list[FaceBox]]:
    rng = random.Random(seed)
    frames: list[list[FaceBox]] = []
    for i in range(total):
        faces = [_jitter(CAM_FACE, rng)] if i < hit_frames else []
        # in-game faces (NPCs, cutscenes) wander around the frame and never cluster
        faces.append(FaceBox(x=rng.randint(0, 1400), y=rng.randint(0, 700), w=90, h=100, score=0.8))
        frames.append(faces)
    return frames


def test_stable_facecam_is_found_with_a_median_box() -> None:
    det = fc.pick_facecam(_frames(30), W, H)
    assert det.status == "found"
    assert (det.hits, det.samples) == (30, 40)
    assert det.face is not None
    assert det.box is not None
    assert abs(det.face.x - CAM_FACE.x) <= 3
    assert abs(det.face.y - CAM_FACE.y) <= 3
    b = det.box
    assert abs(b.w / b.h - fc.PANEL_ASPECT) < 0.02
    assert b.w % 2 == 0
    assert b.h % 2 == 0
    assert b.x >= 0
    assert b.y >= 0
    assert b.x + b.w <= W
    assert b.y + b.h <= H
    # the face sits inside the box
    assert b.x <= det.face.x
    assert det.face.x + det.face.w <= b.x + b.w
    assert b.y <= det.face.y
    assert det.face.y + det.face.h <= b.y + b.h


def test_no_facecam_when_the_face_is_seen_too_rarely() -> None:
    det = fc.pick_facecam(_frames(10), W, H)  # 25 % < MIN_HIT_RATIO
    assert det.status == "none"
    assert det.box is None
    assert "no facecam found" in det.summary()


def test_full_screen_faces_and_weak_detections_are_ignored() -> None:
    big = [[FaceBox(x=600, y=100, w=500, h=600, score=0.95)] for _ in range(40)]  # full-cam scene
    weak = [[FaceBox(x=1580, y=760, w=100, h=110, score=0.3)] for _ in range(40)]
    assert fc.pick_facecam(big, W, H).status == "none"
    assert fc.pick_facecam(weak, W, H).status == "none"


def test_pad_face_keeps_the_box_inside_the_frame_at_a_corner() -> None:
    box = fc.pad_face(PxBox(x=1850, y=1000, w=60, h=70), W, H)
    assert box.x + box.w <= W
    assert box.y + box.h <= H
    assert abs(box.w / box.h - fc.PANEL_ASPECT) < 0.02
    huge = fc.pad_face(PxBox(x=100, y=0, w=900, h=1000), W, H)
    assert huge.w <= W
    assert huge.h <= H


def test_tiles_cover_the_whole_frame() -> None:
    tiles = fc.tiles(W, H)
    assert {side for _, _, side in tiles} == {432}
    covered = np.zeros((H, W), dtype=bool)
    for x, y, side in tiles:
        covered[y : y + side, x : x + side] = True
    assert covered.all()


def _marker_detector(rgb: Any) -> list[FaceBox]:
    """A fake detector: the 'face' is the bounding box of pure-white pixels in the image it is given."""
    ys, xs = np.nonzero(rgb[:, :, 0] == 255)
    if len(xs) == 0:
        return []
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    if x0 == 0 or y0 == 0 or x1 == rgb.shape[1] or y1 == rgb.shape[0]:
        return []  # cut by the tile edge: a real detector would not see a whole face either
    return [FaceBox(x=float(x0), y=float(y0), w=float(x1 - x0), h=float(y1 - y0), score=0.9)]


def test_tiled_detection_maps_back_to_frame_pixels_and_drops_duplicates() -> None:
    rgb = np.zeros((H, W, 3), dtype=np.uint8)
    rgb[760:870, 1580:1680] = 255
    faces = fc.detect_tiled(rgb, _marker_detector, W, H)
    assert len(faces) == 1  # several overlapping tiles saw it; NMS keeps one
    f = faces[0]
    assert (f.x, f.y, f.w, f.h) == (1580, 760, 100, 110)


def test_detect_facecam_end_to_end_with_fake_frames(tmp_path: Path) -> None:
    def grab(path: Path, t: float, w: int, h: int) -> Any:
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[760:870, 1580:1680] = 255
        return rgb

    det = fc.detect_facecam(tmp_path / "v.mp4", W, H, 3600.0, detector=_marker_detector, grab=grab, n=8)
    assert det is not None
    assert det.status == "found"
    assert det.face == PxBox(x=1580, y=760, w=100, h=110)


def test_unreadable_media_stores_nothing(tmp_path: Path) -> None:
    det = fc.detect_facecam(tmp_path / "v.mp4", W, H, 3600.0, detector=_marker_detector, grab=lambda *a: None, n=8)
    assert det is None


def test_sample_times_skip_the_edges() -> None:
    times = fc.sample_times(1000.0, 40)
    assert len(times) == 40
    assert times[0] == pytest.approx(50.0)
    assert times[-1] == pytest.approx(950.0)


def test_presence_ratio_and_median_face() -> None:
    face = FaceBox(x=170, y=60, w=100, h=120, score=0.9)
    frames: list[list[FaceBox]] = [[face]] * 7 + [[]] * 2 + [[FaceBox(x=0, y=0, w=10, h=10, score=0.9)]]
    p = fc.presence_from_faces(frames, 446, 264)
    assert p.ratio == pytest.approx(0.7)  # the tiny face does not count
    assert p.samples == 10
    assert p.face == PxBox(x=170, y=60, w=100, h=120)
    empty = fc.presence_from_faces([], 446, 264)
    assert empty.ratio == 0.0
    assert empty.problem


def test_face_presence_reports_unreadable_media(tmp_path: Path) -> None:
    def grab(path: Path, box: PxBox, start: float, end: float) -> list[Any]:
        raise OSError("ffmpeg frame read failed")

    p = fc.face_presence(tmp_path / "v.mp4", PxBox(x=0, y=0, w=446, h=264), 0, 30, detector=_marker_detector, grab=grab)
    assert p.ratio == 0.0
    assert "ffmpeg" in p.problem


def test_manual_box_in_percent_becomes_a_panel_shaped_box() -> None:
    box = fc.pct_to_px(FacecamBoxPct(x_pct=75, y_pct=65, w_pct=25, h_pct=35), W, H)
    assert box.x >= 1440
    assert box.y >= 702
    assert box.x + box.w <= W
    assert box.y + box.h <= H
    assert abs(box.w / box.h - fc.PANEL_ASPECT) < 0.02


def test_manual_box_must_stay_inside_the_frame() -> None:
    with pytest.raises(ValueError, match="outside the frame"):
        FacecamBoxPct(x_pct=80, y_pct=10, w_pct=30, h_pct=20)


def test_detection_round_trips_through_json() -> None:
    det = fc.pick_facecam(_frames(30), W, H)
    again = fc.FacecamDetection.from_json(det.to_json())
    assert again == det
    assert again.version == fc.DETECTOR_VERSION


def test_spec_resolves_detected_and_manual_boxes() -> None:
    detected = fc.FacecamSpec(origin="detected", box=PxBox(x=1400, y=700, w=446, h=264))
    assert detected.resolve(W, H) == PxBox(x=1400, y=700, w=446, h=264)
    manual = fc.FacecamSpec(origin="campaign", box_pct=FacecamBoxPct(x_pct=0, y_pct=0, w_pct=25, h_pct=25))
    box = manual.resolve(W, H)  # 480x270 trimmed to the panel aspect around its centre
    assert (box.x, box.y, box.h) == (13, 0, 270)
    assert box.x + box.w <= 480
    with pytest.raises(ValueError, match="no box"):
        fc.FacecamSpec(origin="detected").resolve(W, H)
