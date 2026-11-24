from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import EXAMPLE_CONFIG, REPO_ROOT
from tests.render._media import needs_ffmpeg


def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_render", REPO_ROOT / "scripts" / "bench_render.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_window_starts_spread_evenly() -> None:
    b = bench()
    assert b.window_starts(90.0, 30.0, 3) == [0.0, 30.0, 60.0]
    assert b.window_starts(90.0, 30.0, 1) == [0.0]
    with pytest.raises(ValueError, match="longer than the source"):
        b.window_starts(20.0, 30.0, 2)


def test_synthetic_words_cover_the_window_without_overlap() -> None:
    words = bench().synthetic_words(10.0, 70.0)
    assert len(words) == 150  # 2.5 words/s
    assert words[0][1] == 10.0
    assert all(a_end <= b_start for (_, _, a_end), (_, b_start, _) in zip(words, words[1:], strict=False))
    assert all(start < end for _, start, end in words)
    assert words[-1][2] <= 70.0


def test_project_nightly_adds_per_source_and_per_clip_work() -> None:
    proj = bench().project_nightly(
        audio_s_per_source_min=0.5,
        render_s_per_clip=100.0,
        sources=2,
        source_minutes=60.0,
        clips_per_source=8,
        extra_s_per_source=30.0,
    )
    assert proj["per_source_stages_s"] == 0.5 * 60 * 2 + 30 * 2  # 120 s
    assert proj["render_s"] == 100.0 * 8 * 2  # 1600 s
    assert proj["total_s"] == proj["per_source_stages_s"] + proj["render_s"]


def test_timers_accumulate_per_stage() -> None:
    t = bench().Timers()
    wrapped = t.wrap("encode", lambda x: x * 2)
    assert wrapped(3) == 6
    assert wrapped(4) == 8
    assert set(t.seconds) == {"encode"}
    assert t.seconds["encode"] >= 0.0


def test_missing_ffmpeg_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    b = bench()
    monkeypatch.setattr(b.shutil, "which", lambda _: None)
    assert b.main([]) == 1


@needs_ffmpeg
def test_bench_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = bench().main(["--windows", "1", "--length", "10", "--config", str(EXAMPLE_CONFIG), "--out", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "seconds per output second:" in out
    assert "projection:" in out
    assert (tmp_path / "clip_1.mp4").is_file()
