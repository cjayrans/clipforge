"""Committed fixtures are present and valid."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from clipforge.schemas import Transcript
from tests.conftest import FIXTURES


@pytest.mark.parametrize(
    ("name", "min_wpm", "max_wpm"), [("transcript_talk.json", 100, 200), ("transcript_sparse.json", 0, 19)]
)
def test_transcript_fixtures(name: str, min_wpm: float, max_wpm: float) -> None:
    t = Transcript.model_validate(json.loads((FIXTURES / name).read_text()))
    assert t.duration_s == 90.0
    wpm = len(t.words) / (t.duration_s / 60)
    assert min_wpm <= wpm <= max_wpm
    starts = [w.start_s for w in t.words]
    assert starts == sorted(starts)
    assert all(w.end_s > w.start_s for w in t.words)
    for seg in t.segments:
        assert seg.start_s < seg.end_s <= t.duration_s


def test_video_fixture_exists() -> None:
    video = FIXTURES / "synthetic_90s.mp4"
    assert video.is_file()
    assert 1_000_000 < video.stat().st_size < 10_000_000


@pytest.mark.needs_ffmpeg
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe not installed (CI and Docker have it)")
def test_video_fixture_streams() -> None:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height,r_frame_rate",
            "-of",
            "json",
            str(FIXTURES / "synthetic_90s.mp4"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    info = json.loads(out.stdout)
    assert float(info["format"]["duration"]) == pytest.approx(90.0, abs=0.1)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"], video["r_frame_rate"]) == (1920, 1080, "30/1")
    assert any(s["codec_type"] == "audio" for s in info["streams"])
