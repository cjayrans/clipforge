"""Layouts (render/layout.py, render/clip.py): candidate order, geometry, text placement per layout, fallbacks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clipforge import compliance
from clipforge.config import AppConfig
from clipforge.qa.probe import ProbeResult
from clipforge.render import campaign_overlay as co
from clipforge.render import clip as clip_mod
from clipforge.render import layout as lay
from clipforge.render.encode import RenderError, maps_only_source_audio
from clipforge.render.facecam import FacecamSpec, Presence
from clipforge.render.saliency import MotionSample
from clipforge.schemas import CampaignIn, CampaignOverlay, FacecamBoxPct, PxBox, Rect
from tests.render._media import needs_ffmpeg
from tests.render.test_clip import Fakes, request
from tests.support.factories import campaign_in
from tests.support.overlay_png import FAKE_OVERLAY_REL, write_overlay_png

FACECAM = PxBox(x=1400, y=700, w=444, h=264)  # source px (1920x1080)
FACE_IN_CROP = PxBox(x=172, y=64, w=100, h=110)  # a face centred in the facecam crop
GOOD = Presence(ratio=0.9, samples=60, face=FACE_IN_CROP)


def boeshi_like(**kw: Any) -> CampaignOverlay:
    """Same proportions and position as the first campaign that has an overlay (a wide channel banner at the seam)."""
    data: dict[str, Any] = {"path": FAKE_OVERLAY_REL, "position": {"x_pct": 50, "y_pct": 31.5}, "scale": 0.6}
    data.update(kw)
    return CampaignOverlay.model_validate(data)


@pytest.fixture
def at_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    write_overlay_png(root, size=(2172, 724), margin=(100, 140))
    monkeypatch.setattr(clip_mod, "project_root", lambda: root)
    return root


def stacked_request(tmp_path: Path, **kw: Any) -> Any:
    base: dict[str, Any] = {
        "content_kind": "game",
        "facecam": FacecamSpec(origin="detected", box=FACECAM),
    }
    base.update(kw)
    return request(tmp_path, **base)


def graph_of(fakes: Fakes) -> str:
    assert fakes.args is not None
    return fakes.args[fakes.args.index("-filter_complex") + 1]


# --- pure ---------------------------------------------------------------------------------------------------


def test_candidate_order() -> None:
    assert lay.candidates("auto", "game", True) == ["stacked", "crop", "fit"]
    assert lay.candidates("auto", "game", False) == ["crop", "fit"]
    assert lay.candidates("stacked", "game", False) == ["stacked"]
    assert lay.candidates("fit", "game", True) == ["fit"]
    assert lay.candidates("auto", "talk", True) == ["crop"]
    assert lay.candidates("fit", "talk", False) == ["fit"]


def test_gameplay_crop_sizes() -> None:
    assert lay.gameplay_crop("stacked", 1920, 1080) == (910, 1080)
    assert lay.gameplay_crop("crop", 1920, 1080) == (606, 1080)
    assert lay.gameplay_crop("crop", 1280, 720) == (404, 720)
    assert lay.croppable("crop", 1920, 1080)
    assert not lay.croppable("crop", 1080, 1920)
    w, h = lay.gameplay_crop("stacked", 800, 1080)  # narrow source: full width, shorter crop
    assert w == 800
    assert h < 1080


def test_face_rect_maps_into_the_top_panel_with_padding() -> None:
    r = lay.face_rect_out(FACE_IN_CROP, FACECAM)
    assert 350 <= r.x0 <= 380
    assert 700 <= r.x1 <= 720
    assert 95 <= r.y0 <= 110
    assert 470 <= r.y1 <= 480
    assert r.y1 <= lay.FACECAM_PANEL_H


def test_stacked_texts_with_a_banner_at_the_seam(at_root: Path, app_config: AppConfig) -> None:
    styles = list(app_config.styles.values())
    ov = co.load_placement(boeshi_like(), at_root)
    face = lay.face_rect_out(FACE_IN_CROP, FACECAM)
    t = co.place_texts("Ad", ov, styles, face=face, panel=lay.facecam_panel())
    # hook below the banner (not on the face), label in the facecam panel's top-left corner, out of the top UI
    assert t.hook_rect.y0 > ov.opaque_rect.y1
    assert not t.hook_rect.intersects(face)
    assert t.hook_rect.y1 < t.caption_rect.y0  # worst-case 3-line hook still clears the captions
    assert t.ad.rect.y0 >= compliance.UI_TOP_PX
    assert t.ad.rect.y1 < lay.FACECAM_PANEL_H
    assert not t.ad.rect.intersects(face)
    assert not t.ad.rect.intersects(ov.opaque_rect)
    assert not ov.opaque_rect.intersects(face)


def test_label_moves_under_the_hook_when_the_face_is_in_the_corner(app_config: AppConfig) -> None:
    styles = list(app_config.styles.values())
    face = lay.face_rect_out(PxBox(x=5, y=60, w=60, h=60), FACECAM)
    t = co.place_texts("Ad", None, styles, face=face, panel=lay.facecam_panel())
    assert t.ad.rect.y0 > t.hook_rect.y1
    assert not t.ad.rect.intersects(face)


def test_default_layout_texts_are_unchanged(app_config: AppConfig) -> None:
    styles = list(app_config.styles.values())
    t = co.place_texts("Ad", None, styles)
    assert t.hook_cy == co.DEFAULT_HOOK_CY
    assert (t.ad.x, t.ad.y) == (48, 548)
    assert t.ad == co.place_ad_label("Ad", None, styles)


# --- render_candidate ---------------------------------------------------------------------------------------


def test_stacked_layout_render(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    fakes.presence = GOOD
    m = fakes.render(stacked_request(tmp_path), app_config)
    assert m.layout == "stacked"
    assert m.face_rect is not None
    assert m.facecam_rect == lay.facecam_panel()
    assert m.hook_rect is not None
    assert m.ad_label_rect is not None
    assert not m.hook_rect.intersects(m.face_rect)
    assert not m.ad_label_rect.intersects(m.face_rect)
    assert m.crop_plan is not None
    assert all(c.mode == "stacked" and c.facecam == FACECAM for c in m.crop_plan.shots)
    graph = graph_of(fakes)
    assert graph.count("vstack=inputs=2") == 2
    assert graph.count("crop=444:264:1400:700,scale=1080:640") == 2
    assert graph.count("crop=w=910:h=1080:x=505:y=0,scale=1080:1280") == 2  # stationary centre gameplay
    assert fakes.args is not None
    assert maps_only_source_audio(fakes.args)
    doc = (tmp_path / "out" / "clip_7.ass").read_text()
    assert f"\\pos(540,{m.hook_rect.y0 + (m.hook_rect.y1 - m.hook_rect.y0) // 2})" in doc
    assert "facecam_presence" in m.timings
    assert "encode" in m.timings
    assert "total" in m.timings
    compliance.assert_render_manifest(m)


def test_stacked_layout_with_a_banner(tmp_path: Path, app_config: AppConfig, at_root: Path) -> None:
    fakes = Fakes()
    fakes.presence = GOOD
    m = fakes.render(stacked_request(tmp_path, overlay=boeshi_like()), app_config)
    assert m.layout == "stacked"
    assert m.overlay is not None
    assert m.hook_rect is not None
    assert m.hook_rect.y0 > m.overlay.opaque_rect.y1
    assert m.overlay.full_duration
    assert m.overlay.start_s == 0.0
    compliance.assert_render_manifest(m)


def test_stacked_is_skipped_when_the_facecam_shows_no_face(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    fakes.presence = Presence(ratio=0.2, samples=60, face=FACE_IN_CROP)
    m = fakes.render(stacked_request(tmp_path), app_config)
    assert m.layout == "crop"
    assert "stacked: a face is in the facecam box in 20% of 60 sampled frames" in m.layout_reason
    assert m.face_rect is None
    compliance.assert_render_manifest(m)


def test_stacked_is_skipped_when_the_banner_would_cover_the_face(
    tmp_path: Path, app_config: AppConfig, at_root: Path
) -> None:
    fakes = Fakes()
    fakes.presence = Presence(ratio=0.9, samples=60, face=PxBox(x=150, y=150, w=140, h=110))  # low in the cam
    m = fakes.render(stacked_request(tmp_path, overlay=boeshi_like()), app_config)
    assert m.layout == "crop"
    assert "stacked: campaign overlay: overlay visible area" in m.layout_reason
    assert "streamer's face" in m.layout_reason


def test_stacked_is_skipped_when_the_label_has_no_room(tmp_path: Path, app_config: AppConfig, at_root: Path) -> None:
    fakes = Fakes()
    fakes.presence = Presence(ratio=0.9, samples=60, face=PxBox(x=5, y=60, w=60, h=60))  # face in the label corner
    m = fakes.render(stacked_request(tmp_path, overlay=boeshi_like()), app_config)
    assert m.layout == "crop"
    assert "no clear place for the 'Ad' label" in m.layout_reason


def test_forced_layouts_are_refused_when_they_cannot_be_used(tmp_path: Path, app_config: AppConfig) -> None:
    with pytest.raises(RenderError, match="no layout fits this clip: stacked: no facecam"):
        Fakes().render(request(tmp_path, content_kind="game", layout="stacked"), app_config)
    tall = ProbeResult(
        duration_s=90.0, size_bytes=1, width=1080, height=1920, fps=30.0, video_codec="h264", audio_codec="aac",
        audio_streams=1, video_streams=1,
    )  # fmt: skip
    with pytest.raises(RenderError, match="crop: source is not wider than 9:16"):
        Fakes(src=tall).render(request(tmp_path, content_kind="game", layout="crop"), app_config)
    m = Fakes(src=tall).render(request(tmp_path, content_kind="game"), app_config)  # auto falls back to fit
    assert m.layout == "fit"


def test_campaign_facecam_box_is_used(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    fakes.presence = GOOD
    spec = FacecamSpec(origin="campaign", box_pct=FacecamBoxPct(x_pct=72.9, y_pct=64.8, w_pct=23.1, h_pct=24.4))
    m = fakes.render(request(tmp_path, content_kind="game", facecam=spec), app_config)
    assert m.layout == "stacked"
    assert m.crop_plan is not None
    box = m.crop_plan.shots[0].facecam
    assert box is not None
    assert box.x + box.w <= 1920
    assert box.y + box.h <= 1080


def test_tracking_crop_render(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    bins = 192

    def spot(x: float) -> tuple[float, ...]:
        c = x / 1920 * bins
        p = [1.0 if abs(i + 0.5 - c) <= 3 else 0.0 for i in range(bins)]
        return tuple(v / sum(p) for v in p)

    # second shot (clip seconds 10-30): the action sweeps from left to right
    fakes.motion = [MotionSample(t=10 + i * 0.25, profile=spot(200 + 19 * i)) for i in range(80)]
    m = fakes.render(request(tmp_path, content_kind="game"), app_config)
    assert m.layout == "crop"
    assert m.crop_plan is not None
    first, second = m.crop_plan.shots
    assert first.camera == "stationary"
    assert second.camera == "tracking"
    assert second.keyframes
    assert m.max_pan_px_s == pytest.approx(288.0)
    graph = graph_of(fakes)
    assert "x='if(gte(t," in graph
    assert fakes.args is not None
    assert maps_only_source_audio(fakes.args)
    compliance.assert_render_manifest(m)


def test_motion_pass_failure_falls_back_to_a_centre_crop(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()

    def broken(*args: Any) -> list[MotionSample]:
        raise OSError("motion pass failed")

    fakes.motion_fn = broken  # type: ignore[method-assign]
    m = fakes.render(request(tmp_path, content_kind="game"), app_config)
    assert m.layout == "crop"
    assert m.crop_plan is not None
    assert all(c.camera == "stationary" for c in m.crop_plan.shots)


def test_talk_campaigns_cannot_be_stacked() -> None:
    with pytest.raises(ValueError, match="stacked"):
        campaign_in(content_kind="talk", layout="stacked")
    assert campaign_in(content_kind="game").layout == "auto"
    assert CampaignIn.model_fields["layout"].default == "auto"


def test_face_rect_never_leaves_the_panel() -> None:
    r = lay.face_rect_out(PxBox(x=400, y=230, w=44, h=34), FACECAM)  # face at the crop's bottom-right edge
    assert r == Rect(x0=r.x0, y0=r.y0, x1=min(r.x1, 1080), y1=min(r.y1, 640))
    assert r.x1 <= 1080
    assert r.y1 <= 640


# --- real ffmpeg/libass (CI and the clipforge image) ---------------------------------------------------------


@needs_ffmpeg
def test_real_render_stacked_and_tracking(tmp_path: Path, app_config: AppConfig, at_root: Path) -> None:
    """The stacked filtergraph (split, two crops, vstack) and a moving crop expression run in real ffmpeg, with the
    banner overlay, and the output is a valid 1080x1920 clip."""
    from clipforge.qa.probe import probe
    from clipforge.render.clip import render_candidate

    bins = 192

    def spot(x: float) -> tuple[float, ...]:
        c = x / 1920 * bins
        p = [1.0 if abs(i + 0.5 - c) <= 3 else 0.0 for i in range(bins)]
        return tuple(v / sum(p) for v in p)

    def motion(*args: Any) -> list[MotionSample]:
        return [MotionSample(t=i * 0.25, profile=spot(200 + 30 * i)) for i in range(48)]

    for layout, presence in (("stacked", GOOD), ("crop", Presence(ratio=0.0, samples=0))):
        req = stacked_request(tmp_path / layout, start_s=25.0, end_s=37.0, layout=layout, overlay=boeshi_like())
        m = render_candidate(req, app_config, presence_fn=lambda *a, p=presence: p, motion_fn=motion)
        assert m.layout == layout
        assert m.crop_plan is not None
        assert any(c.camera == "tracking" for c in m.crop_plan.shots), m.layout_reason
        compliance.assert_render_manifest(m)
        out = probe(Path(m.clip_path))
        assert (out.width, out.height) == (1080, 1920)
        assert out.audio_streams == 1
        assert out.duration_s == pytest.approx(12.0, abs=0.15)
