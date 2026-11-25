"""ffmpeg loudness/motion passes: argument builders, parsers, failure tolerance, and one real-ffmpeg run."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from clipforge.ingest import media
from clipforge.rank.media_signals import (
    DB_FLOOR,
    RMS_KEY,
    extract,
    loudness_args,
    motion_args,
    parse_metadata,
    per_second,
)
from clipforge.rank.signals import loudness_events, motion_events
from tests.rank.moment_fakes import gta_profile

PRINT = """frame:0    pts:0       pts_time:0
lavfi.astats.Overall.RMS_level=-31.5
frame:1    pts:8000    pts_time:1
lavfi.astats.Overall.RMS_level=-inf
frame:2    pts:16000   pts_time:2
lavfi.astats.Overall.RMS_level=-12.25
"""


def test_args_read_only_the_source_and_write_nothing() -> None:
    rules = gta_profile().signals
    for args in (loudness_args("/m/src.mp4"), motion_args("/m/src.mp4", rules)):
        assert args[0] == "ffmpeg"
        assert args[-3:] == ["-f", "null", "-"]
        assert args[args.index("-i") + 1] == "/m/src.mp4"
        assert "file=-" in " ".join(args)
    assert "-skip_frame" in motion_args("x", rules)
    full = motion_args("x", rules.model_copy(update={"motion_keyframes_only": False}))
    assert "-skip_frame" not in full
    assert "fps=2," in " ".join(full)


def test_parse_metadata_and_per_second() -> None:
    samples = parse_metadata(PRINT, RMS_KEY)
    assert samples == [(0.0, -31.5), (1.0, DB_FLOOR), (2.0, -12.25)]
    assert per_second(samples) == [-31.5, DB_FLOOR, -12.25]
    assert parse_metadata("garbage\nlavfi.x=1", RMS_KEY) == []
    assert per_second([]) == []


def test_extract_tolerates_a_failing_pass() -> None:
    def runner(args: Sequence[str]) -> str:
        if "-skip_frame" in args:
            raise media.MediaToolError("ffmpeg failed (exit 1): no video")
        return PRINT

    signals = extract(Path("/m/src.mp4"), gta_profile().signals, runner)
    assert signals.loudness_db == [-31.5, DB_FLOOR, -12.25]
    assert signals.motion is None
    assert signals.problems == ["motion: ffmpeg failed (exit 1): no video"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_real_ffmpeg_finds_the_loud_second_and_the_cut(tmp_path: Path) -> None:
    """Synthetic 40 s file: quiet tone with a loud burst at 20-22 s; video switches pattern at 20 s."""
    src = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=20",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=20",
            "-f", "lavfi", "-i", "sine=f=440:d=40:sample_rate=48000",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v];"
            "[2:a]volume='if(between(t,20,22),1.0,0.02)':eval=frame[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac",
            str(src),
        ],
        check=True,
    )  # fmt: skip
    rules = gta_profile().signals
    signals = extract(src, rules)
    assert signals.problems == []
    assert signals.loudness_db is not None
    assert len(signals.loudness_db) in (40, 41)
    floor = sorted(signals.loudness_db)[len(signals.loudness_db) // 2]
    loud = [k for k, v in enumerate(signals.loudness_db) if v > floor + 20]  # the burst is 34 dB over the tone
    assert loud
    assert set(loud) <= {19, 20, 21, 22}
    assert signals.motion is not None
    assert len(signals.motion) >= 30
    onsets = [e for e in loudness_events(signals.loudness_db, [], rules) if e.kind == "onset"]
    assert any(19 <= e.t <= 21 for e in onsets)
    cuts = motion_events(signals.motion, rules)
    assert any(18 <= e.end_t <= 21.5 for e in cuts)
