from __future__ import annotations

import pytest

from clipforge.render.reframe import FaceBox, crop_width, plan_crops, plan_shot
from clipforge.render.shots import Shot, detect_shots
from clipforge.schemas import CropMode
from tests.conftest import FIXTURES

W, H = 1920, 1080
SHOT = Shot(0.0, 10.0)
CW = crop_width(W, H)  # 606


def face(cx: float, size: float = 200.0) -> FaceBox:
    return FaceBox(x=cx - size / 2, y=300, w=size, h=size)


def test_crop_width_is_even_9_16() -> None:
    assert CW == 606
    assert crop_width(1281, 720) == 404
    assert crop_width(500, 1080) == 500  # never wider than the source


def test_single_face_centred_on_median() -> None:
    frames = [[face(900)], [face(1000)], [face(950)]]
    crop = plan_shot(SHOT, frames, W, H)
    assert crop.mode is CropMode.FACE
    assert (crop.w, crop.h) == (CW, H)
    assert crop.x == round(950 - CW / 2)
    assert (crop.start_s, crop.end_s) == (0.0, 10.0)


def test_largest_face_wins_small_background_face_ignored() -> None:
    frames = [[face(300, 80), face(1500, 300)]] * 5
    crop = plan_shot(SHOT, frames, W, H)
    assert crop.mode is CropMode.FACE
    assert crop.x == round(1500 - CW / 2)


@pytest.mark.parametrize(("cx", "x"), [(10, 0), (1910, W - CW)])
def test_crop_clamped_to_frame(cx: float, x: int) -> None:
    assert plan_shot(SHOT, [[face(cx)]] * 3, W, H).x == x


def test_two_faces_far_apart_fit_blur() -> None:
    frames = [[face(300, 250), face(1600, 240)]] * 5
    assert plan_shot(SHOT, frames, W, H).mode is CropMode.FIT_BLUR


def test_two_faces_close_together_face_crop() -> None:
    frames = [[face(900, 250), face(1200, 240)]] * 5
    assert plan_shot(SHOT, frames, W, H).mode is CropMode.FACE


@pytest.mark.parametrize("frames", [[], [[], [], []], [[face(900)], [], [], [], []]])
def test_no_or_rare_faces_fit_blur(frames: list[list[FaceBox]]) -> None:
    crop = plan_shot(SHOT, frames, W, H)
    assert crop.mode is CropMode.FIT_BLUR
    assert (crop.x, crop.w, crop.h) == (0, W, H)


def test_vertical_source_always_fit_blur() -> None:
    assert plan_shot(SHOT, [[face(540)]] * 3, 1080, 1920).mode is CropMode.FIT_BLUR


def test_plan_crops_talk_uses_detector() -> None:
    shots = [Shot(0, 5), Shot(5, 10)]
    by_shot = {0: [[face(900)]] * 3, 5: [[]] * 3}
    plan = plan_crops(shots, W, H, "talk", lambda s: by_shot[int(s.start_s)])
    assert [c.mode for c in plan.shots] == [CropMode.FACE, CropMode.FIT_BLUR]
    assert (plan.src_w, plan.src_h) == (W, H)


def test_plan_crops_game_is_fit_blur_without_detection() -> None:
    def boom(_: Shot) -> list[list[FaceBox]]:
        raise AssertionError("detector must not run for game footage")

    plan = plan_crops([Shot(0, 5), Shot(5, 10)], W, H, "game", boom)
    assert all(c.mode is CropMode.FIT_BLUR for c in plan.shots)


def test_plan_crops_without_detector_is_fit_blur() -> None:
    plan = plan_crops([Shot(0, 5)], W, H, "talk", None)
    assert plan.shots[0].mode is CropMode.FIT_BLUR


def test_plan_crops_requires_shots() -> None:
    with pytest.raises(ValueError, match="no shots"):
        plan_crops([], W, H, "talk", None)


def test_faceless_fixture_defaults_to_fit_blur() -> None:
    """PRD F16 on the real face-less fixture: MediaPipe finds no face, every shot is fit_blur."""
    pytest.importorskip("mediapipe")
    from clipforge.render.face_detect import MediaPipeFaceDetector

    path = FIXTURES / "synthetic_90s.mp4"
    shots = detect_shots(path, 25.0, 35.0)
    with MediaPipeFaceDetector(path) as detector:
        frames = detector(shots[0])
        assert frames  # sampled something
        plan = plan_crops(shots, W, H, "talk", detector)
    assert all(c.mode is CropMode.FIT_BLUR for c in plan.shots)
