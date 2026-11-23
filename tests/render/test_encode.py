from __future__ import annotations

import pytest

from clipforge.compliance import ComplianceError
from clipforge.config import AppConfig
from clipforge.render.encode import (
    EncodeSettings,
    RenderError,
    build_ffmpeg_args,
    build_filtergraph,
    default_plan,
    filter_path,
    maps_only_source_audio,
)
from clipforge.schemas import CropMode, CropPlan, ShotCrop
from tests.compliance._helpers import manifest


def plan() -> CropPlan:
    return CropPlan(
        src_w=1920,
        src_h=1080,
        shots=[
            ShotCrop(start_s=100.0, end_s=110.0, mode=CropMode.FACE, x=657, w=606, h=1080),
            ShotCrop(start_s=110.0, end_s=135.0, mode=CropMode.FIT_BLUR, x=0, w=1920, h=1080),
        ],
    )


def args_for(**kw: object) -> list[str]:
    return build_ffmpeg_args(
        manifest(duration_s=35.0, clip_path="/out/clip_1.mp4"),
        source_path="/media/src.mp4",
        clip_start_s=100.0,
        crop_plan=plan(),
        ass_path="/out/clip_1.ass",
        fonts_dir="/app/assets/fonts",
        **kw,  # type: ignore[arg-type]
    )


def value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_single_input_seeked_to_clip() -> None:
    args = args_for()
    assert args.count("-i") == 1
    assert value(args, "-i") == "/media/src.mp4"
    assert value(args, "-ss") == "100.000"
    assert value(args, "-t") == "35.000"
    assert args[-1] == "/out/clip_1.mp4"


def test_maps_video_label_and_source_audio_only() -> None:
    args = args_for()
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["[v]", "0:a:0"]
    assert maps_only_source_audio(args)


def test_codec_settings_match_architecture(app_config: AppConfig) -> None:
    args = args_for(settings=EncodeSettings.from_config(app_config.render))
    assert value(args, "-c:v") == "libx264"
    assert value(args, "-preset") == "veryfast"
    assert value(args, "-crf") == "21"
    assert value(args, "-pix_fmt") == "yuv420p"
    assert value(args, "-r") == "30"
    assert value(args, "-c:a") == "aac"
    assert value(args, "-b:a") == "128k"
    assert value(args, "-af").startswith("loudnorm=I=-14:TP=-1.5:LRA=11")


def test_filtergraph_per_shot_crop_and_fit_blur() -> None:
    graph = value(args_for(), "-filter_complex")
    assert graph.startswith("[0:v]split=2[s0][s1]")
    assert "[s0]trim=start=0.000:end=10.000,setpts=PTS-STARTPTS,crop=606:1080:657:0,scale=1080:1920" in graph
    assert "[s1]trim=start=10.000:end=35.000" in graph
    assert "boxblur" in graph
    assert "overlay=(W-w)/2:(H-h)/2[v1]" in graph
    assert "[v0][v1]concat=n=2:v=1:a=0,ass=filename=/out/clip_1.ass:fontsdir=/app/assets/fonts,fps=30" in graph
    assert graph.endswith("format=yuv420p[v]")


def test_single_shot_graph_without_ass() -> None:
    graph = build_filtergraph(default_plan(20.0, 5.0), 5.0, 20.0, None, None)
    assert graph.startswith("[0:v]null[s0]")
    assert "ass=" not in graph
    assert "trim=start=0.000:end=20.000" in graph


def test_last_shot_extends_to_clip_end() -> None:
    p = CropPlan(
        src_w=1920, src_h=1080, shots=[ShotCrop(start_s=0, end_s=29.5, mode=CropMode.FIT_BLUR, x=0, w=1920, h=1080)]
    )
    assert "end=30.000" in build_filtergraph(p, 0.0, 30.0, None, None)


def test_shot_outside_clip_rejected() -> None:
    shots = [ShotCrop(start_s=a, end_s=a + 10, mode=CropMode.FIT_BLUR, x=0, w=1, h=1) for a in (50, 60)]
    p = CropPlan(src_w=1920, src_h=1080, shots=shots)
    with pytest.raises(RenderError, match="outside the clip"):
        build_filtergraph(p, 0.0, 30.0, None, None)


def test_empty_plan_rejected() -> None:
    with pytest.raises(RenderError, match="no shots"):
        build_filtergraph(CropPlan(src_w=1, src_h=1, shots=[]), 0.0, 30.0, None, None)


def test_manifest_only_defaults_to_fit_blur() -> None:
    args = build_ffmpeg_args(manifest())
    assert "boxblur" in value(args, "-filter_complex")
    assert maps_only_source_audio(args)


@pytest.mark.parametrize("duration", [5.0, 95.0])
def test_duration_bounds_enforced(duration: float) -> None:
    with pytest.raises(ComplianceError):
        build_ffmpeg_args(manifest(duration_s=duration))


def test_resolution_enforced() -> None:
    with pytest.raises(RenderError, match="1080x1920"):
        build_ffmpeg_args(manifest(width=720))


@pytest.mark.parametrize("bad", ["/tmp/a:b.ass", "/tmp/it's.ass", "/tmp/a,b.ass", "/tmp/[x].ass"])
def test_filter_path_refuses_special_characters(bad: str) -> None:
    with pytest.raises(RenderError, match="characters"):
        filter_path(bad)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: [*a[:-1], "-i", "music.mp3", a[-1]],
        lambda a: [x if x != "0:a:0" else "1:a:0" for x in a],
        lambda a: [x if x != "0:a:0" else "[aout]" for x in a],
        lambda a: [*a[:-1], "-filter_complex", "amix=inputs=2", a[-1]],
        lambda a: [x for i, x in enumerate(a) if not (x == "-map" and a[i + 1] == "0:a:0") and x != "0:a:0"],
        lambda a: [*a[:-1], "-map", "0", a[-1]],
        lambda a: [x.replace("[0:v]", "movie=music.mp3[m];[0:v]") for x in a],
        lambda a: [x.replace("[0:v]", "[0:a]anull[x];[0:v]") for x in a],
        lambda a: [x if x != "-filter_complex" else "-filter_complex_script" for x in a],
        lambda a: ["-stream_loop", "-1", *a],
        lambda a: [x if not x.startswith("loudnorm") else "amovie=music.mp3,loudnorm" for x in a],
    ],
)
def test_maps_only_source_audio_detects_violations(mutate: object) -> None:
    args = args_for()
    assert not maps_only_source_audio(mutate(args))  # type: ignore[operator]


def test_path_words_do_not_trip_audio_check() -> None:
    args = build_ffmpeg_args(
        manifest(clip_path="/data/clips/movie-night-sine/clip_1.mp4"),
        ass_path="/data/clips/movie-night-sine/clip_1.ass",
        fonts_dir="/app/assets/join/fonts",
    )
    assert maps_only_source_audio(args)
