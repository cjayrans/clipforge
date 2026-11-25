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


def _gray(seed: int, static: bool = False) -> Any:
    """A half-size grey frame: random texture (a live face and scene change between samples), or the same texture
    every time (a printed face on a wall)."""
    return np.random.default_rng(0 if static else seed).integers(0, 255, (H // 2, W // 2), dtype=np.uint8)


def _frames(
    hit_frames: int, total: int = 40, seed: int = 1, *, static: bool = False, kind: str = "gameplay"
) -> list[fc.FrameSample]:
    rng = random.Random(seed)
    frames: list[fc.FrameSample] = []
    for i in range(total):
        faces = [_jitter(CAM_FACE, rng)] if i < hit_frames else []
        # in-game faces (NPCs, cutscenes) wander around the frame and never cluster
        faces.append(FaceBox(x=rng.randint(0, 1400), y=rng.randint(0, 700), w=90, h=100, score=0.8))
        frames.append(fc.FrameSample(t=float(i), kind=kind, faces=tuple(faces), gray=_gray(i, static)))  # type: ignore[arg-type]
    return frames


def test_stable_facecam_is_found_with_a_median_box() -> None:
    det = fc.pick_facecam(_frames(30), W, H)
    assert det.status == "found"
    assert (det.hits, det.samples, det.gameplay, det.fullcam) == (30, 40, 40, 0)
    assert det.variation is not None
    assert det.variation > fc.MIN_FACE_VARIATION
    assert det.debug_t == 0.0
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
    det = fc.pick_facecam(_frames(20), W, H)  # 50 % < MIN_HIT_RATIO
    assert det.status == "none"
    assert det.box is None
    assert "no facecam found" in det.summary()


def test_weak_detections_are_ignored() -> None:
    weak = [
        fc.FrameSample(t=float(i), kind="gameplay", faces=(FaceBox(x=1580, y=760, w=100, h=110, score=0.3),))
        for i in range(40)
    ]
    assert fc.pick_facecam(weak, W, H).status == "none"


# --- failure modes fixed in v2 -------------------------------------------------------------------------------


def test_static_printed_face_is_not_a_facecam() -> None:
    """A face in a picture on the wall: found in every gameplay frame at the same place, but it never changes."""
    det = fc.pick_facecam(_frames(40, static=True), W, H)
    assert det.status == "none"
    assert det.box is None
    assert det.face is not None  # the candidate is reported, for the debug image
    assert det.variation is not None
    assert det.variation < fc.MIN_FACE_VARIATION
    assert "a picture, not a person" in det.reason
    assert "a picture, not a person" in det.summary()


def test_full_screen_camera_frames_do_not_count() -> None:
    """A full-screen camera segment: those samples are classified fullcam and ignored; with too few gameplay samples
    the source has no facecam, whatever the background contains."""
    fullcam = _frames(40, kind="fullcam", static=True)
    det = fc.pick_facecam(fullcam, W, H)
    assert det.status == "none"
    assert (det.gameplay, det.fullcam) == (0, 40)
    assert "only 0 gameplay frames" in det.reason
    mixed = _frames(30, total=30) + _frames(10, total=10, kind="fullcam", seed=9)
    found = fc.pick_facecam(mixed, W, H)
    assert found.status == "found"
    assert (found.gameplay, found.fullcam, found.hits) == (30, 10, 30)


def test_classify_frame() -> None:
    assert fc.classify_frame([FaceBox(x=600, y=100, w=300, h=300, score=0.9)], H) == "fullcam"  # 28 % of 1080
    assert fc.classify_frame([FaceBox(x=600, y=100, w=200, h=200, score=0.9)], H) == "gameplay"
    assert fc.classify_frame([FaceBox(x=600, y=100, w=300, h=300, score=0.2)], H) == "gameplay"
    assert fc.classify_frame([], H) == "gameplay"


def test_face_variation() -> None:
    rng = np.random.default_rng(5)
    live = [rng.integers(0, 255, (110, 100), dtype=np.uint8) for _ in range(10)]
    printed = [live[0].copy() for _ in range(10)]
    brighter = [np.clip(live[0].astype(int) + 10 * i, 0, 255).astype(np.uint8) for i in range(10)]
    assert fc.face_variation(live) > 15  # far above MIN_FACE_VARIATION
    assert fc.face_variation(printed) == pytest.approx(0.0)
    assert fc.face_variation(brighter) < fc.MIN_FACE_VARIATION  # a lighting change alone is not a live face
    assert fc.face_variation(live[:1]) is None


def test_samples_are_taken_inside_the_clip_windows() -> None:
    times = fc.sample_times_in_windows([(100, 130), (500, 510), (120, 140)], n=10)
    assert len(times) == 10
    assert all(100 <= t <= 140 or 500 <= t <= 510 for t in times)
    assert sum(500 <= t <= 510 for t in times) == 2  # in proportion: 10 s of 50 s
    assert fc.sample_times_in_windows([], 10) == []


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


def _video(kind: str, *, static_face: bool = False) -> Any:
    """Synthetic fixture frames. "gameplay": a dark game scene with a small facecam face (a white outline, the marker
    the fake detector finds) whose inside changes every frame (a live face) or never (`static_face`: a printed face).
    "fullcam": a full-screen camera shot, the face 40 % of the frame height."""

    def grab(path: Path, t: float, w: int, h: int) -> Any:
        rng = np.random.default_rng(int(t * 1000))
        rgb = rng.integers(0, 60, (h, w, 3), dtype=np.uint8)  # the game scene changes
        if kind == "fullcam":
            rgb[300:732, 760:1160] = 255
            rgb[302:730, 762:1158] = rng.integers(0, 200, (428, 396, 3), dtype=np.uint8)
            return rgb
        rgb[760:870, 1580:1680] = 255
        inner = np.random.default_rng(7 if static_face else int(t * 1000) + 1)
        rgb[762:868, 1582:1678] = inner.integers(0, 200, (106, 96, 3), dtype=np.uint8)
        return rgb

    return grab


def test_detect_facecam_end_to_end_with_fake_frames(tmp_path: Path) -> None:
    det = fc.detect_facecam(tmp_path / "v.mp4", W, H, 3600.0, detector=_marker_detector, grab=_video("gameplay"), n=12)
    assert det is not None
    assert det.status == "found"
    assert det.face == PxBox(x=1580, y=760, w=100, h=110)


def test_end_to_end_static_printed_face_fixture(tmp_path: Path) -> None:
    det = fc.detect_facecam(
        tmp_path / "v.mp4", W, H, 3600.0, detector=_marker_detector, grab=_video("gameplay", static_face=True), n=12
    )
    assert det is not None
    assert det.status == "none"
    assert "a picture, not a person" in det.reason


def test_end_to_end_full_screen_camera_fixture(tmp_path: Path) -> None:
    det = fc.detect_facecam(tmp_path / "v.mp4", W, H, 3600.0, detector=_marker_detector, grab=_video("fullcam"), n=12)
    assert det is not None
    assert det.status == "none"
    assert (det.gameplay, det.fullcam) == (0, 12)


def test_detection_samples_only_the_clip_windows(tmp_path: Path) -> None:
    """A VOD whose first half is a full-screen camera segment and second half gameplay with a facecam: sampling the
    clip windows (all in the gameplay half) finds the facecam; sampling the whole VOD sees mostly the camera."""
    gameplay, fullcam = _video("gameplay"), _video("fullcam")

    def grab(path: Path, t: float, w: int, h: int) -> Any:
        return fullcam(path, t, w, h) if t < 1800 else gameplay(path, t, w, h)

    windows = [(2000.0, 2030.0), (2500.0, 2520.0), (3100.0, 3140.0)]
    det = fc.detect_facecam(
        tmp_path / "v.mp4", W, H, 3600.0, detector=_marker_detector, grab=grab, n=12, windows=windows
    )
    assert det is not None
    assert det.status == "found"
    assert det.debug_t is not None
    assert det.debug_t >= 2000


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
    assert p.variation is None  # no crops given
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


def test_presence_measures_the_face_variation() -> None:
    face = FaceBox(x=170, y=60, w=100, h=120, score=0.9)
    rng = np.random.default_rng(1)
    live = [rng.integers(0, 255, (264, 446, 3), dtype=np.uint8) for _ in range(6)]
    printed = [live[0]] * 6
    assert fc.presence_from_faces([[face]] * 6, 446, 264, live).variation > fc.MIN_FACE_VARIATION  # type: ignore[operator]
    assert fc.presence_from_faces([[face]] * 6, 446, 264, printed).variation == pytest.approx(0.0)


def test_clip_fullcam_ratio(tmp_path: Path) -> None:
    ratio = fc.clip_fullcam_ratio(
        tmp_path / "v.mp4", 0, 30, W, H, detector=_marker_detector, grab=_video("fullcam"), n=4
    )
    assert ratio == 1.0
    ratio = fc.clip_fullcam_ratio(
        tmp_path / "v.mp4", 0, 30, W, H, detector=_marker_detector, grab=_video("gameplay"), n=4
    )
    assert ratio == 0.0
    assert (
        fc.clip_fullcam_ratio(tmp_path / "v.mp4", 0, 30, W, H, detector=_marker_detector, grab=lambda *a: None) is None
    )
