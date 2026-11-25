"""Campaign overlay image (generic, fake campaign): placement, clearance, "Ad" label position, encoder and render."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clipforge import compliance
from clipforge.config import AppConfig
from clipforge.render import campaign_overlay as co
from clipforge.render.encode import (
    OverlayInput,
    RenderError,
    build_ffmpeg_args,
    build_filtergraph,
    default_plan,
    maps_only_source_audio,
)
from clipforge.schemas import CampaignOverlay, Rect
from tests.compliance._helpers import manifest
from tests.render.test_clip import Fakes, request
from tests.support.overlay_png import FAKE_OVERLAY_REL, write_overlay_png


def overlay_cfg(**kw: Any) -> CampaignOverlay:
    data: dict[str, Any] = {"path": FAKE_OVERLAY_REL, "position": {"x_pct": 50, "y_pct": 31.5}, "scale": 0.5}
    data.update(kw)
    return CampaignOverlay.model_validate(data)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    write_overlay_png(tmp_path)  # 800x200, opaque box x 40-760, y 30-170
    return tmp_path


def placement(root: Path, **kw: Any) -> co.OverlayPlacement:
    cfg = overlay_cfg(**kw)
    return co.place_overlay(cfg, co.read_image(root / cfg.path), root / cfg.path)


# --- image + placement (pure) ------------------------------------------------------------------------------


def test_read_image_finds_the_visible_box(root: Path) -> None:
    info = co.read_image(root / FAKE_OVERLAY_REL)
    assert (info.width, info.height) == (800, 200)
    assert info.opaque == (40, 30, 760, 170)
    assert len(info.sha256) == 64


def test_read_image_refuses_png_without_alpha(tmp_path: Path) -> None:
    path = write_overlay_png(tmp_path, alpha=False)
    with pytest.raises(co.OverlayPlacementError, match="no alpha"):
        co.read_image(path)


def test_read_image_refuses_non_png(tmp_path: Path) -> None:
    path = tmp_path / "x.png"
    path.write_bytes(b"GIF89a not a png")
    with pytest.raises(co.OverlayPlacementError, match="not a PNG"):
        co.read_image(path)


def test_place_overlay_scales_and_centres(root: Path) -> None:
    p = placement(root)
    assert (p.width, p.height) == (540, 136)  # 0.5 x 1080, aspect kept, even sizes
    assert p.rect.x0 == 270
    assert p.rect.x1 == 810
    assert p.rect.y0 <= round(0.315 * 1920) <= p.rect.y1
    # The visible box sits inside the image rect, shrunk by the transparent margins (never smaller than real).
    o = p.opaque_rect
    assert p.rect.x0 < o.x0 < o.x1 < p.rect.x1
    assert p.rect.y0 < o.y0 < o.y1 < p.rect.y1
    assert o.x0 == 270 + 27
    assert o.x1 == 270 + 513


def test_clear_placement_has_no_problems(root: Path, app_config: AppConfig) -> None:
    assert co.clearance_problems(placement(root), app_config.styles.values()) == []


@pytest.mark.parametrize(
    ("kw", "what"),
    [
        ({"position": {"x_pct": 50, "y_pct": 66}}, "captions"),
        ({"position": {"x_pct": 50, "y_pct": 18}}, "hook"),
        ({"position": {"x_pct": 50, "y_pct": 90}}, "bottom 20 %"),
        ({"position": {"x_pct": 80, "y_pct": 31.5}}, "right 15 %"),
        ({"position": {"x_pct": 50, "y_pct": 5}, "scale": 0.3}, "top 150 px"),
        ({"position": {"x_pct": 5, "y_pct": 31.5}}, "outside the 1080x1920 frame"),
    ],
)
def test_clearance_refuses_covering(root: Path, app_config: AppConfig, kw: dict[str, Any], what: str) -> None:
    problems = co.clearance_problems(placement(root, **kw), app_config.styles.values())
    assert any(what in p for p in problems), problems


def test_ui_zones_are_tiktok_safe_margins() -> None:
    zones = co.ui_zones()
    assert zones["platform UI (top 150 px)"] == Rect(x0=0, y0=0, x1=1080, y1=150)
    assert zones["platform UI (right 15 %)"].x0 == 918
    assert zones["platform UI (bottom 20 %)"].y0 == 1536


# --- "Ad" label placement --------------------------------------------------------------------------------


def _fake_placement(opaque: Rect) -> co.OverlayPlacement:
    return co.OverlayPlacement(path=Path("x.png"), rect=opaque, opaque_rect=opaque, width=2, height=2, sha256="0")


def test_ad_label_default_left_under_the_hook(app_config: AppConfig) -> None:
    ad = co.place_ad_label("Ad", None, app_config.styles.values())
    assert (ad.alignment, ad.x, ad.y) == (7, 48, 548)
    hook = co.hook_band(app_config.styles.values())
    assert ad.rect.y0 >= hook.y1 + co.MARGIN_PX  # under the hook's worst case (3 lines, largest preset)
    assert ad.rect.y0 >= co.UI_TOP_PX  # below TikTok's top 150 px
    assert ad.rect.y1 <= co.caption_band(app_config.styles.values()).y0 - co.MARGIN_PX
    assert co.ad_label_problems(ad.rect, app_config.styles.values(), None) == []


def test_ad_label_stays_put_when_overlay_is_clear(root: Path, app_config: AppConfig) -> None:
    ad = co.place_ad_label("Ad", placement(root), app_config.styles.values())
    assert (ad.alignment, ad.x, ad.y) == (7, 48, 548)
    assert not ad.rect.intersects(placement(root).opaque_rect)


def test_ad_label_moves_under_an_overlay_in_its_spot(app_config: AppConfig) -> None:
    covering = Rect(x0=0, y0=520, x1=400, y1=640)
    ad = co.place_ad_label("Ad", _fake_placement(covering), app_config.styles.values())
    assert (ad.alignment, ad.x) == (7, 48)
    assert ad.y == 640 + co.MARGIN_PX + 12
    assert not ad.rect.intersects(covering)


def test_ad_label_refused_when_no_spot_is_clear(app_config: AppConfig) -> None:
    with pytest.raises(co.OverlayPlacementError, match="no clear place for the 'Ad' label"):
        co.place_ad_label("Ad", _fake_placement(Rect(x0=0, y0=500, x1=400, y1=1200)), app_config.styles.values())


def test_ad_label_follows_a_bigger_hook_style(app_config: AppConfig) -> None:
    """A larger style preset pushes the hook's worst case lower; the label moves under it, never into it."""
    data = app_config.model_dump(by_alias=True)
    data["styles"]["huge"] = {**data["styles"]["bold-yellow"], "size": 110}
    styles = type(app_config).model_validate(data).styles.values()
    ad = co.place_ad_label("Ad", None, styles)
    assert ad.rect.y0 >= co.hook_band(styles).y1 + co.MARGIN_PX
    assert co.ad_label_problems(ad.rect, styles, None) == []


def test_label_style_has_a_dark_box() -> None:
    from dataclasses import replace

    from clipforge.render.overlays import ad_label_style, has_dark_box

    style = ad_label_style("Montserrat-ExtraBold")
    assert has_dark_box(style)
    assert style.outline_colour == "&H40000000"  # 75 % opaque black box
    assert not has_dark_box(replace(style, border_style=1))  # outline only, no box
    assert not has_dark_box(replace(style, outline_colour="&HC0000000"))  # box mostly transparent
    assert not has_dark_box(replace(style, outline_colour="&H40FFFFFF"))  # white box
    assert not has_dark_box(replace(style, outline=2))  # no padding


# --- resolve (files) -------------------------------------------------------------------------------------


def test_resolve_ok(root: Path, app_config: AppConfig) -> None:
    p = co.resolve(overlay_cfg(), root, app_config.styles.values(), "Ad")
    assert p.path == (root / FAKE_OVERLAY_REL).resolve()


def test_resolve_missing_file(tmp_path: Path, app_config: AppConfig) -> None:
    with pytest.raises(co.OverlayPlacementError, match="not found"):
        co.resolve(overlay_cfg(), tmp_path, app_config.styles.values(), "Ad")


def test_resolve_refuses_symlink_out_of_assets(tmp_path: Path, app_config: AppConfig) -> None:
    outside = write_overlay_png(tmp_path, "elsewhere/banner.png")
    link = tmp_path / FAKE_OVERLAY_REL
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    with pytest.raises(co.OverlayPlacementError, match="outside campaigns/assets"):
        co.resolve(overlay_cfg(), tmp_path, app_config.styles.values(), "Ad")


def test_resolve_never_moves_a_bad_placement(root: Path, app_config: AppConfig) -> None:
    with pytest.raises(co.OverlayPlacementError, match="captions"):
        co.resolve(overlay_cfg(position={"x_pct": 50, "y_pct": 66}), root, app_config.styles.values(), "Ad")


@pytest.mark.parametrize(
    "path", ["../campaigns/assets/x.png", "/abs/campaigns/assets/x.png", "assets/x.png", "campaigns/assets/x.jpg"]
)
def test_schema_refuses_paths_outside_assets(path: str) -> None:
    with pytest.raises(ValueError, match="overlay.path"):
        overlay_cfg(path=path)


# --- encoder ---------------------------------------------------------------------------------------------

OVL = OverlayInput(path="/x/campaigns/assets/fake-campaign/banner.png", width=540, height=136, x=270, y=537)


def test_filtergraph_composites_overlay_before_ass() -> None:
    plan = default_plan(20.0, 10.0)
    graph = build_filtergraph(plan, 10.0, 20.0, "/tmp/c.ass", None, 30, OVL)
    assert "[1:v]scale=540:136,format=rgba[ovl]" in graph
    assert "[base][ovl]overlay=x=270:y=537:eof_action=repeat,ass=filename=/tmp/c.ass" in graph
    assert graph.index("overlay=x=270") < graph.index("ass=filename")  # label/hook/captions drawn on top
    assert "enable=" not in graph  # full duration


def test_filtergraph_overlay_after_hook() -> None:
    plan = default_plan(20.0, 10.0)
    late = OverlayInput(path="o.png", width=540, height=136, x=270, y=537, start_s=2.5)
    graph = build_filtergraph(plan, 10.0, 20.0, None, None, 30, late)
    assert "enable='between(t,2.500,20.000)'" in graph


def test_overlay_args_keep_source_audio_only() -> None:
    args = build_ffmpeg_args(manifest(duration_s=20.0), source_path="src.mp4", overlay=OVL)
    assert args.count("-i") == 2
    assert args[args.index("-i", args.index("-i") + 1) + 1] == str(OVL.path)
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["[v]", "0:a:0"]
    assert maps_only_source_audio(args)


@pytest.mark.parametrize(
    "second",
    ["music.mp3", "other.mp4", "voice.wav", "concat:a.png", "http://x/o.png", "pipe:0.png"],
)
def test_second_input_must_be_a_local_png(second: str) -> None:
    args = build_ffmpeg_args(manifest(duration_s=20.0), source_path="src.mp4", overlay=OVL)
    i = args.index(str(OVL.path))
    assert not maps_only_source_audio([*args[:i], second, *args[i + 1 :]])


def test_overlay_input_cannot_carry_audio_into_the_output() -> None:
    args = build_ffmpeg_args(manifest(duration_s=20.0), source_path="src.mp4", overlay=OVL)
    assert not maps_only_source_audio([*args[:-1], "-map", "1:a", args[-1]])
    j = args.index("-filter_complex") + 1
    assert not maps_only_source_audio([*args[:j], args[j] + ";[1:a]anull[x]", *args[j + 1 :]])
    k = args.index("-i")
    assert not maps_only_source_audio([*args[:k], "-f", "lavfi", *args[k:]])
    assert not maps_only_source_audio([*args[:k], "-loop", "1", *args[k:]])
    assert not maps_only_source_audio([*args[:-1], "-i", "third.png", args[-1]])


# --- render_candidate with a campaign overlay (mocked ffmpeg) -------------------------------------------


@pytest.fixture
def at_root(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import clipforge.render.clip as clip_mod

    monkeypatch.setattr(clip_mod, "project_root", lambda: root)
    return root


def test_render_with_overlay(tmp_path: Path, app_config: AppConfig, at_root: Path) -> None:
    fakes = Fakes()
    m = fakes.render(request(tmp_path, overlay=overlay_cfg(), content_kind="game"), app_config)
    assert fakes.args is not None
    assert str((at_root / FAKE_OVERLAY_REL).resolve()) in fakes.args
    graph = fakes.args[fakes.args.index("-filter_complex") + 1]
    assert graph.index("overlay=x=") < graph.index("ass=filename")
    assert m.overlay is not None
    assert m.overlay.full_duration
    assert m.overlay.start_s == 0.0
    assert m.overlay.end_s == m.duration_s
    assert m.ad_label_rect is not None
    assert not m.ad_label_rect.intersects(m.overlay.opaque_rect)
    assert m.audio_source_only
    compliance.assert_render_manifest(m)


def test_render_without_overlay_is_unchanged(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    m = fakes.render(request(tmp_path), app_config)
    assert fakes.args is not None
    assert fakes.args.count("-i") == 1
    assert m.overlay is None
    assert m.ad_label_rect is not None


def test_render_refused_when_overlay_would_cover_captions(tmp_path: Path, app_config: AppConfig, at_root: Path) -> None:
    fakes = Fakes()
    bad = overlay_cfg(position={"x_pct": 50, "y_pct": 66})
    with pytest.raises(RenderError, match="campaign overlay: .*captions"):
        fakes.render(request(tmp_path, overlay=bad), app_config)
    assert fakes.args is None  # ffmpeg never ran


def test_render_refused_when_overlay_missing(tmp_path: Path, app_config: AppConfig, at_root: Path) -> None:
    (at_root / FAKE_OVERLAY_REL).unlink()
    with pytest.raises(RenderError, match="not found"):
        Fakes().render(request(tmp_path, overlay=overlay_cfg()), app_config)


def test_unlabeled_or_numbered_input_pads_refused() -> None:
    """An unlabeled `[1]` or `[1:1]` pad could pull audio out of the second input; only [0:v]/[1:v] are allowed."""
    args = build_ffmpeg_args(manifest(duration_s=20.0), source_path="src.mp4", overlay=OVL)
    j = args.index("-filter_complex") + 1
    for extra in (";[1]anull", ";[1:1]aresample=48000", ";[0]anull", ";[0:1]anull", ";[2:v]null"):
        assert not maps_only_source_audio([*args[:j], args[j] + extra, *args[j + 1 :]]), extra


def test_overlay_input_opened_with_streams_blocked() -> None:
    args = build_ffmpeg_args(manifest(duration_s=20.0), source_path="src.mp4", overlay=OVL)
    second = [i for i, a in enumerate(args) if a == "-i"][1]
    assert args[second - 3 : second] == ["-an", "-sn", "-dn"]
    assert not maps_only_source_audio([a for a in args if a != "-an"])


def test_overlay_swapped_after_check_refuses_render(
    tmp_path: Path, app_config: AppConfig, at_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clipforge.render.clip as clip_mod

    real = clip_mod.build_ffmpeg_args

    def swap(*a: Any, **kw: Any) -> list[str]:
        (at_root / FAKE_OVERLAY_REL).write_bytes(b"\x89PNG\r\n\x1a\n swapped")
        return real(*a, **kw)

    monkeypatch.setattr(clip_mod, "build_ffmpeg_args", swap)
    fakes = Fakes()
    with pytest.raises(RenderError, match="changed after it was checked"):
        fakes.render(request(tmp_path, overlay=overlay_cfg()), app_config)
    assert fakes.args is None
