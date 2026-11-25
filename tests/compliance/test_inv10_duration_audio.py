"""Invariant 10: clips are 10-90 s and contain no added music track (only source audio is mapped)."""

from __future__ import annotations

import pytest

from clipforge.compliance import ComplianceError, assert_duration_bounds, assert_render_manifest
from clipforge.config import AppConfig
from tests.compliance._helpers import manifest


@pytest.mark.parametrize(("duration", "ok"), [(10.0, True), (90.0, True), (45.0, True), (9.99, False), (90.01, False)])
def test_duration_bounds(duration: float, ok: bool) -> None:
    if ok:
        assert_duration_bounds(duration)
        assert_render_manifest(manifest(duration_s=duration))
    else:
        with pytest.raises(ComplianceError):
            assert_duration_bounds(duration)
        with pytest.raises(ComplianceError):
            assert_render_manifest(manifest(duration_s=duration))


def test_config_clip_bounds_within_floor(app_config: AppConfig) -> None:
    assert app_config.pipeline.min_clip_s >= 10
    assert app_config.pipeline.max_clip_s <= 90


def test_only_source_audio_mapped() -> None:
    with pytest.raises(ComplianceError, match="audio"):
        assert_render_manifest(manifest(audio_source_only=False))


def test_encoder_maps_only_source_audio() -> None:
    from clipforge.render.encode import build_ffmpeg_args  # type: ignore[attr-defined]

    args = build_ffmpeg_args(manifest())
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    audio_maps = [m for m in maps if ":a" in m]
    assert audio_maps
    assert all(m.startswith("0:a") for m in audio_maps)
    assert args.count("-i") == 1 or all("music" not in a for a in args)


def test_qa_rejects_out_of_bounds_duration() -> None:
    from clipforge.qa.checks import duration_check  # type: ignore[attr-defined]

    assert not duration_check(95.0).passed
    assert duration_check(30.0).passed


def test_campaign_overlay_input_adds_no_audio() -> None:
    """A campaign overlay is a second input, but only a local .png still; audio still comes only from input 0."""
    from clipforge.render.encode import OverlayInput, build_ffmpeg_args, maps_only_source_audio

    ovl = OverlayInput(path="campaigns/assets/fake/banner.png", width=540, height=136, x=270, y=537)
    args = build_ffmpeg_args(manifest(duration_s=30.0), overlay=ovl)
    assert maps_only_source_audio(args)
    audio_maps = [args[i + 1] for i, a in enumerate(args) if a == "-map" and args[i + 1] != "[v]"]
    assert audio_maps == ["0:a:0"]
    swapped = [("music.mp3" if a == ovl.path else a) for a in args]
    assert not maps_only_source_audio(swapped)


def test_overlay_input_pads_and_flags() -> None:
    """The overlay is opened with -an -sn -dn, and no filter pad may name an input stream other than [0:v]/[1:v]."""
    from clipforge.render.encode import OverlayInput, build_ffmpeg_args, maps_only_source_audio

    ovl = OverlayInput(path="campaigns/assets/fake/banner.png", width=540, height=136, x=270, y=537)
    args = build_ffmpeg_args(manifest(duration_s=30.0), overlay=ovl)
    assert not maps_only_source_audio([a for a in args if a != "-an"])
    j = args.index("-filter_complex") + 1
    for extra in (";[1]anull", ";[1:1]aresample=48000", ";[0]anull", ";[ 1:a]anull", ";[0x1]anull", ";[ 0:a]anull"):
        assert not maps_only_source_audio([*args[:j], args[j] + extra, *args[j + 1 :]])
