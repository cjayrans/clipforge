"""TEMPORARY diagnostic (removed in the next commit): what does a real 480p proxy of the fixture weigh?"""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.qa.preview import proxy_args
from clipforge.qa.probe import probe
from clipforge.render.encode import run_ffmpeg
from tests.conftest import FIXTURES
from tests.render._media import needs_ffmpeg


@needs_ffmpeg
def test_measure_proxy_sizes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = FIXTURES / "synthetic_90s.mp4"
    lines = [f"MEASURE source bytes={src.stat().st_size}"]
    for crf in ("23", "28", "32"):
        out = tmp_path / f"p{crf}.mp4"
        args = proxy_args(src, out)
        args[args.index("-crf") + 1] = crf
        run_ffmpeg(args)
        p = probe(out)
        lines.append(f"MEASURE crf={crf} bytes={out.stat().st_size} {p.width}x{p.height} dur={p.duration_s:.2f}")
    with capsys.disabled():
        print("\n" + "\n".join(lines))
