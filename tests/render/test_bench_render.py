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


def test_missing_ffmpeg_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    b = bench()
    monkeypatch.setattr(b.shutil, "which", lambda _: None)
    assert b.main([]) == 1


@needs_ffmpeg
def test_bench_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = bench().main(["--windows", "1", "--length", "10", "--config", str(EXAMPLE_CONFIG), "--out", str(tmp_path)])
    assert rc == 0
    assert "seconds per output second:" in capsys.readouterr().out
    assert (tmp_path / "clip_1.mp4").is_file()
