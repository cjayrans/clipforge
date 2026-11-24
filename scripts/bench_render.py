"""Nightly-run benchmark (TASKS C7 / P3.1): per-stage timings and a projection for a 2 × 1 h night.

    uv run python scripts/bench_render.py [SOURCE] [--windows 4] [--length 60] [--kind talk|game]
        [--transcript words.json] [--account gaming-1] [--config config/clipforge.example.yaml] [--out DIR]
        [--audio] [--json FILE] [--label NAME] [--clips-per-source 8] [--sources 2] [--source-minutes 60]

SOURCE defaults to tests/fixtures/synthetic_90s.mp4. Run it inside the Docker image with the worker's CPU limit
(`docker run --cpus 6 ...`) for real numbers: ffmpeg, libass and mediapipe differ from the Mac and from CI. Footage
under tests/fixtures/local/ is git-ignored; never commit campaign footage.

Stages timed (download is never included):
- `hash`    SHA-256 of the whole source (ingest registers every file by hash). Only with `--audio`.
- `audio`   audio extraction + chunk cutting for transcription (`transcribe.audio`), whole source. Only with `--audio`.
- per rendered clip: `shots` (PySceneDetect), `face` (MediaPipe sampling, `--kind talk` only), `encode` (the one
  ffmpeg run: trim/crop/blur, libass burn-in of captions + hook + "Ad" label, x264, loudnorm), `probe` (ffprobe),
  `qa_io` (the QA gate's own re-probe + SHA-256 of the clip), `other` (everything else in `render_candidate`).
Without `--transcript` every window gets synthetic caption words (~2.5 words/s) so the libass caption path is
exercised the way it is on a real clip; the words carry no meaning and nothing about the source is read.

Not measured here (they leave the machine or cost money): the Groq upload and call, and the Anthropic ranking calls.
Target (PRD §6): a nightly run for 2 × 1 h sources in < 3 h on the Mac.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = REPO / "tests" / "fixtures" / "synthetic_90s.mp4"
DEFAULT_CONFIG = REPO / "config" / "clipforge.example.yaml"

RENDER_STAGES = ("shots", "face", "encode", "probe", "qa_io", "other")
WORDS_PER_SECOND = 2.5  # ~150 wpm, the pace of dense commentary


def window_starts(source_s: float, length: float, n: int) -> list[float]:
    """n windows of `length` spread evenly over the source (pure)."""
    if length > source_s:
        raise ValueError(f"window {length}s is longer than the source ({source_s:.1f}s)")
    if n <= 1:
        return [0.0]
    step = (source_s - length) / (n - 1)
    return [round(i * step, 3) for i in range(n)]


def synthetic_words(
    start_s: float, end_s: float, words_per_s: float = WORDS_PER_SECOND
) -> list[tuple[str, float, float]]:
    """(text, start, end) tuples, evenly spaced over [start_s, end_s) (pure). Placeholder text for caption timing."""
    n = max(1, int((end_s - start_s) * words_per_s))
    step = (end_s - start_s) / n
    return [(f"word{i % 7}", round(start_s + i * step, 3), round(start_s + (i + 1) * step - 0.04, 3)) for i in range(n)]


def project_nightly(
    *,
    audio_s_per_source_min: float,
    render_s_per_clip: float,
    sources: int = 2,
    source_minutes: float = 60.0,
    clips_per_source: int = 8,
    extra_s_per_source: float = 0.0,
) -> dict[str, float]:
    """Seconds for one night (pure): per-source-minute work × minutes, plus per-clip work × clips (pure).

    `clips_per_source` is the worst case: every candidate of a source is rendered (`pipeline.top_n_per_source`);
    the pipeline stops early once `nightly_candidate_target` clips pass QA.
    """
    audio = audio_s_per_source_min * source_minutes * sources
    render = render_s_per_clip * clips_per_source * sources
    extra = extra_s_per_source * sources
    return {"per_source_stages_s": audio + extra, "render_s": render, "total_s": audio + extra + render}


class Timers:
    """Accumulates seconds per stage name."""

    def __init__(self) -> None:
        self.seconds: dict[str, float] = defaultdict(float)

    @contextlib.contextmanager
    def span(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += time.perf_counter() - t0

    def wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def inner(*a: Any, **k: Any) -> Any:
            with self.span(name):
                return fn(*a, **k)

        return inner


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("source", nargs="?", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--windows", type=int, default=3)
    p.add_argument("--length", type=float, default=30.0)
    p.add_argument("--kind", choices=["talk", "game"], default="game")
    p.add_argument("--transcript", type=Path, help="schemas.Transcript JSON for captions (default: synthetic words)")
    p.add_argument("--account", default="gaming-1")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--out", type=Path, help="keep renders here (default: temp dir, deleted)")
    p.add_argument("--audio", action="store_true", help="also time hashing and audio extraction of the whole source")
    p.add_argument("--json", type=Path, help="write the results as JSON")
    p.add_argument("--label", default="", help="free text stored in the JSON (e.g. 'baseline')")
    p.add_argument("--sources", type=int, default=2, help="projection: sources per night")
    p.add_argument("--source-minutes", type=float, default=60.0, help="projection: minutes per source")
    p.add_argument("--clips-per-source", type=int, default=8, help="projection: clips rendered per source (worst case)")
    return p.parse_args(argv)


def time_audio_stages(source: Path, work: Path) -> dict[str, float]:
    """Seconds to hash the source and to extract + chunk its audio, exactly as ingest and transcribe do."""
    from clipforge.ingest import media
    from clipforge.qa.probe import probe
    from clipforge.transcribe import audio
    from clipforge.transcribe.chunker import plan_chunks

    t0 = time.perf_counter()
    media.sha256_file(source)
    hash_s = time.perf_counter() - t0
    duration = probe(source).duration_s
    t0 = time.perf_counter()
    mp3 = audio.extract_audio(source, work / "audio.mp3")
    audio.cut_chunks(mp3, plan_chunks(duration), work / "chunks")
    return {"hash": hash_s, "audio": time.perf_counter() - t0, "source_s": duration}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe not found; run inside the Docker image", file=sys.stderr)
        return 1

    from clipforge.compliance import AD_LABEL_MIN_END_S
    from clipforge.config import load_app_config
    from clipforge.qa.probe import probe
    from clipforge.render.clip import ClipRequest, mediapipe_detector, render_candidate
    from clipforge.render.encode import run_ffmpeg, sha256_file
    from clipforge.render.shots import detect_shots
    from clipforge.schemas import Transcript, Word

    app = load_app_config(args.config)
    transcript_words: list[Word] | None = None
    if args.transcript:
        transcript_words = Transcript.model_validate(json.loads(args.transcript.read_text())).words
    source_s = probe(args.source).duration_s
    starts = window_starts(source_s, args.length, args.windows)

    tmp = tempfile.TemporaryDirectory(prefix="bench_render_")
    out_dir = args.out or Path(tmp.name) / "clips"
    result: dict[str, Any] = {
        "label": args.label,
        "source": args.source.name,
        "source_s": source_s,
        "kind": args.kind,
        "windows": [],
        "render": app.render.model_dump() if hasattr(app.render, "model_dump") else {},
    }
    try:
        print(f"source {args.source.name} ({source_s:.0f}s), {len(starts)} × {args.length:g}s, kind={args.kind}")
        if args.audio:
            audio_t = time_audio_stages(args.source, Path(tmp.name) / "audio")
            minutes = audio_t["source_s"] / 60
            result["audio_stages"] = {
                "hash_s": audio_t["hash"],
                "audio_s": audio_t["audio"],
                "hash_s_per_source_min": audio_t["hash"] / minutes,
                "audio_s_per_source_min": audio_t["audio"] / minutes,
            }
            print(
                f"  hash {audio_t['hash']:.1f}s ({audio_t['hash'] / minutes:.2f} s/source-min), "
                f"audio extract+chunk {audio_t['audio']:.1f}s ({audio_t['audio'] / minutes:.2f} s/source-min)"
            )

        total = Timers()
        total_wall = total_out = 0.0
        for i, start in enumerate(starts):
            end = start + args.length
            words = transcript_words
            if words is None:
                words = [Word(text=t, start_s=a, end_s=b) for t, a, b in synthetic_words(start, end)]
            req = ClipRequest(
                candidate_id=i + 1, source_path=args.source, start_s=start, end_s=end, words=words,
                hook="Benchmark hook text here", content_kind=args.kind, account_set=args.account, out_dir=out_dir,
            )  # fmt: skip
            t = Timers()

            @contextlib.contextmanager
            def timed_detector(path: Path, t: Timers = t) -> Iterator[Any]:
                with contextlib.ExitStack() as stack:
                    with t.span("face"):  # model load
                        real = stack.enter_context(mediapipe_detector(path))
                    # plan_crops calls the detector once per shot; time each call.
                    yield None if real is None else t.wrap("face", real)

            t0 = time.perf_counter()
            manifest = render_candidate(
                req,
                app,
                detector_factory=timed_detector,
                shots_fn=t.wrap("shots", detect_shots),
                probe_fn=t.wrap("probe", probe),
                run=t.wrap("encode", run_ffmpeg),
            )
            wall = time.perf_counter() - t0
            with t.span("qa_io"):
                probe(manifest.clip_path)
                sha256_file(manifest.clip_path)
            parts = dict(t.seconds)
            parts["other"] = max(0.0, wall - sum(v for k, v in parts.items() if k != "qa_io"))
            for k, v in parts.items():
                total.seconds[k] += v
            total_wall += wall + parts["qa_io"]
            total_out += manifest.duration_s
            if not (
                manifest.has_ad_label
                and manifest.ad_label_start_s == 0.0
                and manifest.ad_label_end_s >= AD_LABEL_MIN_END_S
            ):
                raise SystemExit("benchmark render lost the 'Ad' label (invariant 3); refusing to report timings")
            result["windows"].append({"start_s": start, "wall_s": wall, "out_s": manifest.duration_s, **parts})
            print(f"  window {i + 1}: {start:8.1f}s  {wall:6.1f}s wall  {wall / manifest.duration_s:5.2f} s/s")

        per_out_s = {k: total.seconds.get(k, 0.0) / total_out for k in RENDER_STAGES}
        result["s_per_output_second"] = per_out_s
        result["s_per_output_second_total"] = total_wall / total_out
        for k in RENDER_STAGES:
            print(f"    {k:7s} {per_out_s[k]:6.3f} s per output second")
        print(f"seconds per output second: {total_wall / total_out:.2f}")

        render_s_per_clip = total_wall / total_out * args.length
        audio_per_min = result.get("audio_stages", {}).get("audio_s_per_source_min", 0.0)
        hash_per_min = result.get("audio_stages", {}).get("hash_s_per_source_min", 0.0)
        proj = project_nightly(
            audio_s_per_source_min=audio_per_min,
            render_s_per_clip=render_s_per_clip,
            sources=args.sources,
            source_minutes=args.source_minutes,
            clips_per_source=args.clips_per_source,
            extra_s_per_source=hash_per_min * args.source_minutes,
        )
        result["projection"] = {
            **proj,
            "render_s_per_clip": render_s_per_clip,
            "clip_s": args.length,
            "sources": args.sources,
            "source_minutes": args.source_minutes,
            "clips_per_source": args.clips_per_source,
        }
        print(
            f"projection: {args.sources} × {args.source_minutes:g} min, {args.clips_per_source} clips of "
            f"{args.length:g}s each: {proj['total_s'] / 60:.1f} min "
            f"(per-source stages {proj['per_source_stages_s'] / 60:.1f} min, render {proj['render_s'] / 60:.1f} min)"
        )
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    finally:
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
