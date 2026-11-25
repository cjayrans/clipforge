from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from clipforge.compliance import assert_render_manifest
from clipforge.config import AppConfig
from clipforge.qa.probe import ProbeResult, probe
from clipforge.render.clip import ClipRequest, render_candidate
from clipforge.render.encode import RenderError
from clipforge.render.reframe import Detector, FaceBox
from clipforge.render.shots import Shot
from clipforge.schemas import Transcript
from tests.conftest import FIXTURES
from tests.render._media import needs_ffmpeg

SRC = ProbeResult(
    duration_s=90.0, size_bytes=1, width=1920, height=1080, fps=30.0, video_codec="h264", audio_codec="aac",
    audio_streams=1, video_streams=1,
)  # fmt: skip
OUT = ProbeResult(
    duration_s=30.02, size_bytes=1, width=1080, height=1920, fps=30.0, video_codec="h264", audio_codec="aac",
    audio_streams=1, video_streams=1,
)  # fmt: skip


def transcript() -> Transcript:
    return Transcript.model_validate(json.loads((FIXTURES / "transcript_talk.json").read_text()))


def request(tmp_path: Path, **kw: Any) -> ClipRequest:
    base: dict[str, Any] = {
        "candidate_id": 7,
        "source_path": FIXTURES / "synthetic_90s.mp4",
        "start_s": 20.0,
        "end_s": 50.0,
        "words": transcript().words,
        "hook": "Nobody saw this coming",
        "content_kind": "talk",
        "account_set": "gaming-1",
        "out_dir": tmp_path / "out",
    }
    base.update(kw)
    return ClipRequest(**base)


class Fakes:
    def __init__(self, src: ProbeResult = SRC) -> None:
        self.src = src
        self.args: list[str] | None = None
        self.detector_calls = 0

    def probe(self, path: Path) -> ProbeResult:
        return OUT if path.suffix == ".mp4" and path.name.startswith("clip_") else self.src

    def shots(self, path: Path, start: float, end: float) -> list[Shot]:
        return [Shot(start, 30.0), Shot(30.0, end)]

    def run(self, args: list[str]) -> None:
        self.args = args
        Path(args[-1]).write_bytes(b"fake mp4")

    @contextlib.contextmanager
    def detector(self, path: Path) -> Iterator[Detector]:
        def detect(shot: Shot) -> list[list[FaceBox]]:
            self.detector_calls += 1
            return [[FaceBox(900, 300, 200, 200)]] * 3 if shot.start_s < 30 else [[]]

        yield detect

    def render(self, req: ClipRequest, app: AppConfig) -> Any:
        return render_candidate(
            req, app, detector_factory=self.detector, shots_fn=self.shots, probe_fn=self.probe, run=self.run
        )


def test_render_candidate_manifest(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    m = fakes.render(request(tmp_path), app_config)
    assert_render_manifest(m)
    assert m.clip_path == str(tmp_path / "out" / "clip_7.mp4")
    assert (m.candidate_id, m.account_set, m.style_preset) == (7, "gaming-1", "bold-yellow")
    assert (m.width, m.height, m.fps) == (1080, 1920, 30.0)
    assert m.duration_s == 30.02
    assert (m.has_ad_label, m.ad_label_start_s, m.ad_label_end_s) == (True, 0.0, 3.0)
    assert (m.ad_label_layout, m.ad_label_box) == (2, True)
    assert m.ad_label_rect is not None
    assert m.ad_label_rect.y0 >= 150  # below TikTok's top UI zone
    assert (m.hook_text, m.hook_end_s) == ("Nobody saw this coming", 2.5)
    assert m.audio_source_only
    assert len(m.sha256) == 64
    saved = json.loads((tmp_path / "out" / "clip_7.manifest.json").read_text())
    assert saved["sha256"] == m.sha256


def test_render_candidate_writes_ass_with_overlays_and_captions(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    fakes.render(request(tmp_path), app_config)
    doc = (tmp_path / "out" / "clip_7.ass").read_text()
    assert "Dialogue: 10,0:00:00.00,0:00:03.00,AdLabel,," in doc
    assert ",Hook,," in doc
    assert doc.count(",Caption,,") > 10
    assert "Style: Caption,Montserrat-ExtraBold,78," in doc
    assert fakes.args is not None
    graph = fakes.args[fakes.args.index("-filter_complex") + 1]
    assert "crop=606:1080:697:0" in graph  # face shot
    assert "boxblur" in graph  # face-less shot


def test_game_content_skips_detection(tmp_path: Path, app_config: AppConfig) -> None:
    fakes = Fakes()
    fakes.render(request(tmp_path, content_kind="game"), app_config)
    assert fakes.detector_calls == 0
    assert fakes.args is not None
    graph = fakes.args[fakes.args.index("-filter_complex") + 1]
    assert "crop=606" not in graph
    assert graph.count("boxblur") == 2


def test_silent_source_refused(tmp_path: Path, app_config: AppConfig) -> None:
    silent = ProbeResult(**{**SRC.__dict__, "audio_streams": 0, "audio_codec": None})
    with pytest.raises(RenderError, match="no audio"):
        Fakes(silent).render(request(tmp_path), app_config)


@pytest.mark.parametrize(("start", "end"), [(20.0, 25.0), (0.0, 95.0)])
def test_out_of_bounds_length_refused(tmp_path: Path, app_config: AppConfig, start: float, end: float) -> None:
    with pytest.raises(Exception, match="duration|outside"):
        Fakes().render(request(tmp_path, start_s=start, end_s=end), app_config)


def test_clip_past_source_end_refused(tmp_path: Path, app_config: AppConfig) -> None:
    with pytest.raises(RenderError, match="source is"):
        Fakes().render(request(tmp_path, start_s=70.0, end_s=100.0), app_config)


def test_unknown_account_set_refused(tmp_path: Path, app_config: AppConfig) -> None:
    with pytest.raises(RenderError, match="account set"):
        Fakes().render(request(tmp_path, account_set="nope"), app_config)


def test_blank_ad_label_text_refused(tmp_path: Path, app_config: AppConfig) -> None:
    """Invariant 3: a label that would render invisibly must never be recorded as has_ad_label=True."""
    data = app_config.model_dump(by_alias=True)
    data["render"]["ad_label_text"] = "   "
    with pytest.raises(ValueError, match="ad_label_text"):  # config refuses it at load time (Phase 2)
        AppConfig.model_validate(data)
    # Render keeps its own check (defence in depth): build the config without validation.
    cfg = app_config.model_copy(update={"render": app_config.render.model_copy(update={"ad_label_text": "   "})})
    fakes = Fakes()
    with pytest.raises(RenderError, match="ad_label_text is blank"):
        fakes.render(request(tmp_path), cfg)
    assert fakes.args is None
    assert not (tmp_path / "out" / "clip_7.manifest.json").exists()


def test_extra_audio_input_refused_before_encoding(
    tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 10: if the args ever carried another audio source, render stops before ffmpeg runs."""
    import clipforge.render.clip as clip_mod

    real = clip_mod.build_ffmpeg_args

    def with_music(*a: Any, **kw: Any) -> list[str]:
        args = real(*a, **kw)
        return [*args[:-1], "-i", "music.mp3", "-map", "1:a:0", args[-1]]

    monkeypatch.setattr(clip_mod, "build_ffmpeg_args", with_music)
    fakes = Fakes()
    with pytest.raises(RenderError, match="invariant 10"):
        fakes.render(request(tmp_path), app_config)
    assert fakes.args is None
    assert not (tmp_path / "out" / "clip_7.manifest.json").exists()


def test_manifest_flags_come_from_the_written_ass(tmp_path: Path, app_config: AppConfig) -> None:
    m = Fakes().render(request(tmp_path, hook=",AdLabel,, trick"), app_config)
    doc = (tmp_path / "out" / "clip_7.ass").read_text().splitlines()
    ad_lines = [line for line in doc if line.startswith("Dialogue:") and line.split(",", 4)[3] == "AdLabel"]
    assert len(ad_lines) == 1
    assert ad_lines[0].startswith("Dialogue: 10,0:00:00.00,0:00:03.00,AdLabel,,")
    assert ad_lines[0].endswith("}Ad")
    assert m.has_ad_label


def test_unbundled_font_refused(tmp_path: Path, app_config: AppConfig) -> None:
    data = app_config.model_dump(by_alias=True)
    data["styles"]["bold-yellow"]["font"] = "Nope-Regular"
    cfg = AppConfig.model_validate(data)
    with pytest.raises(RenderError, match="font not bundled"):
        Fakes().render(request(tmp_path), cfg)


@needs_ffmpeg
@pytest.mark.parametrize("kind", ["game", "talk"])
def test_real_render_of_fixture(tmp_path: Path, app_config: AppConfig, kind: str) -> None:
    """End to end on the synthetic fixture with real ffmpeg/libass (CI and Docker)."""
    req = request(tmp_path, start_s=25.0, end_s=37.0, content_kind=kind)
    m = render_candidate(req, app_config)
    assert_render_manifest(m)
    out = probe(m.clip_path)
    assert (out.width, out.height) == (1080, 1920)
    assert out.fps == pytest.approx(30.0)
    assert out.video_codec == "h264"
    assert out.audio_codec == "aac"
    assert out.audio_streams == 1
    assert out.duration_s == pytest.approx(12.0, abs=0.15)


def _gray_region(clip: str, t: float, x: int, y: int, w: int, h: int) -> bytes:
    import subprocess

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{t}", "-i", clip, "-frames:v", "1",
           "-vf", f"crop={w}:{h}:{x}:{y},format=gray", "-f", "rawvideo", "-"]  # fmt: skip
    out = subprocess.run(cmd, check=True, capture_output=True).stdout
    assert len(out) == w * h
    return out


def _mean_abs_diff(a: bytes, b: bytes) -> float:
    return sum(abs(p - q) for p, q in zip(a, b, strict=True)) / len(a)


@needs_ffmpeg
def test_ad_label_pixels_visible_then_gone(
    tmp_path: Path, app_config: AppConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """Invariant 3 on real pixels. Compare the rendered clip with a baseline encode of the same window and crop but
    no ASS: the label box differs strongly at 0.5 s and 2.5 s (label on) and barely at 4 s (label off)."""
    from clipforge.render.encode import EncodeSettings, build_ffmpeg_args, default_plan, run_ffmpeg

    req = request(tmp_path, start_s=40.0, end_s=52.0, content_kind="game", hook="", words=[])
    m = render_candidate(req, app_config)  # 40-52 s is one fit_blur shot of the fixture (cuts at 30 s and 60 s)
    baseline = str(tmp_path / "baseline.mp4")
    run_ffmpeg(
        build_ffmpeg_args(
            m.model_copy(update={"clip_path": baseline}),
            source_path=req.source_path,
            clip_start_s=req.start_s,
            crop_plan=default_plan(m.duration_s, req.start_s),
            ass_path=None,
            settings=EncodeSettings.from_config(app_config.render),
        )
    )
    r = m.ad_label_rect
    assert r is not None
    box = (r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)  # the label's padded box as recorded in the manifest
    diffs = {
        t: _mean_abs_diff(_gray_region(m.clip_path, t, *box), _gray_region(baseline, t, *box)) for t in (0.5, 2.5, 4.0)
    }
    with capsys.disabled():  # shown in the CI log so the margin against the thresholds is visible
        print(f"\nAD-LABEL mean grey diff vs no-ass baseline (label box): {diffs}")
    assert diffs[0.5] > 15, diffs
    assert diffs[2.5] > 15, diffs
    assert diffs[4.0] < 4, diffs


def _bright_source(tmp_path: Path) -> Path:
    """12 s of white 1920x1080 video with a tone: the worst case for a white "Ad" label without a box."""
    import subprocess

    out = tmp_path / "white.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "color=c=white:s=1920x1080:r=30:d=12",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", str(out)]  # fmt: skip
    subprocess.run(cmd, check=True)
    return out


@needs_ffmpeg
def test_ad_label_readable_on_white_footage(
    tmp_path: Path, app_config: AppConfig, capsys: pytest.CaptureFixture[str]
) -> None:
    """On pure white footage the label's dark box makes the area dark and the white "Ad" text stands out on it."""
    src = _bright_source(tmp_path)
    req = request(tmp_path, source_path=src, start_s=0.0, end_s=11.0, content_kind="game", hook="", words=[])
    m = render_candidate(req, app_config, shots_fn=lambda p, a, b: [Shot(a, b)])
    r = m.ad_label_rect
    assert r is not None
    box = (r.x0 + 2, r.y0 + 2, r.x1 - r.x0 - 4, r.y1 - r.y0 - 4)
    on = _gray_region(m.clip_path, 0.5, *box)
    off = _gray_region(m.clip_path, 4.0, *box)
    dark = sorted(on)[len(on) // 4]  # the box (most pixels)
    text = sorted(on)[-len(on) // 20]  # the brightest 5 %: the letters
    mean_off = sum(off) / len(off)
    with capsys.disabled():
        print(f"\nAD-LABEL on white: box quartile {dark}, text 95th pct {text}, area after 3 s {mean_off:.0f}")
    assert dark < 110, dark  # 75 % black over white: about 64 in luma, far from the white background
    assert text > 200, text  # white letters on the dark box
    assert mean_off > 230, mean_off  # gone after 3 s: back to white
