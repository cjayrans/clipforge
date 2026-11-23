from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clipforge.qa.probe import parse_probe, probe
from tests.conftest import FIXTURES
from tests.render._media import needs_ffmpeg

FFPROBE_JSON: dict[str, Any] = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
         "avg_frame_rate": "30/1", "r_frame_rate": "30/1"},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
        {"index": 2, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
    ],
    "format": {"duration": "35.021000", "size": "7340032"},
}  # fmt: skip


def test_parse_probe() -> None:
    r = parse_probe(FFPROBE_JSON)
    assert (r.width, r.height, r.fps) == (1080, 1920, 30.0)
    assert (r.video_codec, r.audio_codec) == ("h264", "aac")
    assert (r.video_streams, r.audio_streams) == (1, 1)  # cover art is not a video stream
    assert r.duration_s == pytest.approx(35.021)
    assert r.size_bytes == 7_340_032


def test_parse_probe_ntsc_rate_and_missing_audio() -> None:
    data = {"streams": [{"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001"}]}
    r = parse_probe(data)
    assert r.fps == pytest.approx(29.97, abs=0.01)
    assert r.audio_streams == 0
    assert r.audio_codec is None
    assert r.duration_s == 0.0


def test_parse_probe_falls_back_to_r_frame_rate() -> None:
    data = {"streams": [{"codec_type": "video", "avg_frame_rate": "0/0", "r_frame_rate": "25/1"}]}
    assert parse_probe(data).fps == 25.0


def test_probe_uses_injected_runner() -> None:
    seen: list[Path | str] = []

    def runner(path: Path | str) -> dict[str, Any]:
        seen.append(path)
        return FFPROBE_JSON

    assert probe("x.mp4", runner).height == 1920
    assert seen == ["x.mp4"]


@needs_ffmpeg
def test_probe_fixture() -> None:
    r = probe(FIXTURES / "synthetic_90s.mp4")
    assert (r.width, r.height) == (1920, 1080)
    assert r.duration_s == pytest.approx(90.0, abs=0.1)
    assert r.audio_streams == 1
