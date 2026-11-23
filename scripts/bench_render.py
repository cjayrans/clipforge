"""Render benchmark (TASKS C7 / P3.1): render N windows of a source and print seconds per output second.

    uv run python scripts/bench_render.py [SOURCE] [--windows 3] [--length 30] [--kind talk|game]
        [--transcript words.json] [--account gaming-1] [--config config/clipforge.example.yaml] [--out DIR]

SOURCE defaults to tests/fixtures/synthetic_90s.mp4. Run it inside the Docker image for real numbers (ffmpeg,
libass, mediapipe). Footage under tests/fixtures/local/ is git-ignored; never commit campaign footage.
Target (PRD §6): a nightly run for 2 × 1 h sources in < 3 h on the Mac.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "tests" / "fixtures" / "synthetic_90s.mp4"
DEFAULT_CONFIG = REPO / "config" / "clipforge.example.yaml"


def window_starts(source_s: float, length: float, n: int) -> list[float]:
    """n windows of `length` spread evenly over the source (pure)."""
    if length > source_s:
        raise ValueError(f"window {length}s is longer than the source ({source_s:.1f}s)")
    if n <= 1:
        return [0.0]
    step = (source_s - length) / (n - 1)
    return [round(i * step, 3) for i in range(n)]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--windows", type=int, default=3)
    p.add_argument("--length", type=float, default=30.0)
    p.add_argument("--kind", choices=["talk", "game"], default="game")
    p.add_argument("--transcript", type=Path, help="schemas.Transcript JSON for captions (optional)")
    p.add_argument("--account", default="gaming-1")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--out", type=Path, help="keep renders here (default: temp dir, deleted)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe not found; run inside the Docker image", file=sys.stderr)
        return 1

    from clipforge.config import load_app_config
    from clipforge.qa.probe import probe
    from clipforge.render.clip import ClipRequest, render_candidate
    from clipforge.schemas import Transcript, Word

    app = load_app_config(args.config)
    words: list[Word] = []
    if args.transcript:
        words = Transcript.model_validate(json.loads(args.transcript.read_text())).words
    source_s = probe(args.source).duration_s
    starts = window_starts(source_s, args.length, args.windows)

    tmp = None if args.out else tempfile.TemporaryDirectory(prefix="bench_render_")
    out_dir = args.out or Path(tmp.name)  # type: ignore[union-attr]
    total_wall = total_out = 0.0
    try:
        print(f"source {args.source.name} ({source_s:.0f}s), {len(starts)} × {args.length:g}s, kind={args.kind}")
        for i, start in enumerate(starts):
            req = ClipRequest(
                candidate_id=i + 1, source_path=args.source, start_s=start, end_s=start + args.length, words=words,
                hook="Benchmark hook text here", content_kind=args.kind, account_set=args.account, out_dir=out_dir,
            )  # fmt: skip
            t0 = time.perf_counter()
            manifest = render_candidate(req, app)
            wall = time.perf_counter() - t0
            total_wall += wall
            total_out += manifest.duration_s
            print(f"  window {i + 1}: {start:8.1f}s  {wall:6.1f}s wall  {wall / manifest.duration_s:5.2f} s/s")
        print(f"seconds per output second: {total_wall / total_out:.2f}")
    finally:
        if tmp is not None:
            tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
