"""QA layout checks (feat/reframe): crop bounds, pan speed, a face in the rendered facecam panel."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clipforge.qa import checks
from clipforge.qa.probe import ProbeResult
from clipforge.schemas import CropKeyframe, CropMode, CropPlan, PxBox, Rect, RenderManifest, ShotCrop
from tests.compliance._helpers import campaign, manifest

OUT = ProbeResult(
    duration_s=35.0, size_bytes=1, width=1080, height=1920, fps=30.0, video_codec="h264", audio_codec="aac",
    audio_streams=1, video_streams=1,
)  # fmt: skip


def crop_shot(**kw: Any) -> ShotCrop:
    data: dict[str, Any] = {"start_s": 0.0, "end_s": 35.0, "mode": CropMode.GAME_CROP, "x": 657, "w": 606, "h": 1080}
    data.update(kw)
    return ShotCrop(**data)


def layout3(*shots: ShotCrop, **kw: Any) -> RenderManifest:
    data: dict[str, Any] = {
        "ad_label_layout": 3,
        "layout": "crop",
        "crop_plan": CropPlan(src_w=1920, src_h=1080, shots=list(shots) or [crop_shot()]),
        "max_pan_px_s": 288.0,
    }
    data.update(kw)
    return manifest(**data)


def names(report: Any) -> dict[str, bool]:
    return {c.name: c.passed for c in report.checks}


def test_crop_inside_the_frame_passes() -> None:
    r = checks.run_qa(layout3(), OUT, campaign(), [], {})
    assert names(r)["crop_bounds"]
    assert names(r)["pan_speed"]


def test_crop_outside_the_frame_fails() -> None:
    bad = layout3(crop_shot(x=1400))
    assert not checks.crop_bounds_check(bad).passed
    moving = layout3(crop_shot(keyframes=[CropKeyframe(t=0, x=0), CropKeyframe(t=10, x=1500)]))
    assert "leaves the frame" in checks.crop_bounds_check(moving).detail


def test_pan_speed_over_the_limit_fails() -> None:
    fast = layout3(crop_shot(keyframes=[CropKeyframe(t=0, x=0), CropKeyframe(t=1, x=400)]))
    check = checks.pan_speed_check(fast)
    assert not check.passed
    assert "400 px/s" in check.detail
    slow = layout3(crop_shot(keyframes=[CropKeyframe(t=0, x=0), CropKeyframe(t=2, x=400)]))
    assert checks.pan_speed_check(slow).passed
    no_limit = layout3(crop_shot(keyframes=[CropKeyframe(t=0, x=0), CropKeyframe(t=2, x=400)]), max_pan_px_s=None)
    assert not checks.pan_speed_check(no_limit).passed


def test_stacked_needs_a_face_in_the_rendered_panel() -> None:
    shot = crop_shot(mode=CropMode.STACKED, w=910, x=505, facecam=PxBox(x=1400, y=700, w=444, h=264))
    m = layout3(shot, layout="stacked")
    assert not names(checks.run_qa(m, OUT, campaign(), [], {}, face_ratio=None))["facecam_face"]  # fails closed
    assert not names(checks.run_qa(m, OUT, campaign(), [], {}, face_ratio=0.2))["facecam_face"]
    assert names(checks.run_qa(m, OUT, campaign(), [], {}, face_ratio=0.8))["facecam_face"]


def test_stacked_facecam_box_must_be_inside_the_source() -> None:
    shot = crop_shot(mode=CropMode.STACKED, w=910, x=505, facecam=PxBox(x=1700, y=700, w=444, h=264))
    assert not checks.crop_bounds_check(layout3(shot, layout="stacked")).passed


def test_older_manifests_get_no_layout_checks() -> None:
    r = checks.run_qa(manifest(), OUT, campaign(), [], {})
    assert not {"crop_bounds", "pan_speed", "facecam_face"} & set(names(r))


def test_qa_clip_samples_the_rendered_facecam_panel(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    from clipforge.render.encode import sha256_file

    shot = crop_shot(mode=CropMode.STACKED, w=910, x=505, facecam=PxBox(x=1400, y=700, w=444, h=264))
    panel = Rect(x0=0, y0=0, x1=1080, y1=640)
    m = layout3(shot, layout="stacked", facecam_rect=panel, clip_path=str(clip), sha256=sha256_file(clip))
    seen: list[tuple[Path, Rect, float]] = []

    def face_check(path: Path, rect: Rect, duration: float) -> float | None:
        seen.append((path, rect, duration))
        return 1.0

    r = checks.qa_clip(m, campaign(), [], {}, probe_fn=lambda p: OUT, face_check_fn=face_check)
    assert seen == [(clip, panel, 35.0)]
    assert names(r)["facecam_face"]
