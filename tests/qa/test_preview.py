from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.qa.preview import TELEGRAM_VIDEO_LIMIT_BYTES, preview_for_telegram, proxy_args
from clipforge.render.encode import RenderError
from tests.conftest import FIXTURES
from tests.render._media import needs_ffmpeg


def test_small_clip_sent_as_is(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 100)

    def never(_: list[str]) -> None:
        raise AssertionError("no proxy for small clips")

    assert preview_for_telegram(clip, run=never) == clip
    assert TELEGRAM_VIDEO_LIMIT_BYTES == 50_000_000


def test_large_clip_gets_480p_proxy(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 200)
    calls: list[list[str]] = []

    def fake(args: list[str]) -> None:
        calls.append(args)
        Path(args[-1]).write_bytes(b"y" * 50)

    out = preview_for_telegram(clip, limit_bytes=100, run=fake)
    assert out == tmp_path / "clip.preview.mp4"
    assert len(calls) == 1
    args = calls[0]
    assert args[args.index("-vf") + 1] == "scale=480:-2"
    assert args.count("-i") == 1
    # Reused while newer than the clip.
    preview_for_telegram(clip, limit_bytes=100, run=fake)
    assert len(calls) == 1


def test_proxy_still_too_large_raises(tmp_path: Path) -> None:
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x" * 200)

    def fake(args: list[str]) -> None:
        Path(args[-1]).write_bytes(b"y" * 150)

    with pytest.raises(RenderError, match="still over"):
        preview_for_telegram(clip, limit_bytes=100, run=fake)


def test_proxy_args_map_source_streams_only() -> None:
    args = proxy_args(Path("/c/clip.mp4"), Path("/c/clip.preview.mp4"))
    maps = [args[i + 1] for i, a in enumerate(args) if a == "-map"]
    assert maps == ["0:v:0", "0:a:0"]
    assert args[-1] == "/c/clip.preview.mp4"


@needs_ffmpeg
def test_real_proxy_from_fixture(tmp_path: Path) -> None:
    from clipforge.qa.probe import probe

    src = FIXTURES / "synthetic_90s.mp4"
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(src.read_bytes())
    out = preview_for_telegram(clip, limit_bytes=1000)
    p = probe(out)
    assert (p.width, p.height) == (480, 270)
    assert p.audio_streams == 1
