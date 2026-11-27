"""Audio extraction and chunk cutting with ffmpeg (ARCHITECTURE §4.2).

Argument builders are pure; the runners take an injectable `runner` (see `clipforge.ingest.media`), so unit tests run
without ffmpeg. Only the first audio stream of the source is used.
"""

from __future__ import annotations

import re
from pathlib import Path

from clipforge.ingest import media
from clipforge.transcribe.chunker import AUDIO_BITRATE_BPS, ChunkPlan, Silence

SAMPLE_RATE = 16_000
SILENCE_NOISE_DB = -35
SILENCE_MIN_S = 0.4
_SILENCE_RE = re.compile(r"lavfi\.silence_(start|end)=(-?[\d.]+)")


def extract_audio_args(src: Path | str, out: Path | str) -> list[str]:
    """`ffmpeg -i in -vn -ac 1 -ar 16000 -b:a 32k out.mp3` (first audio stream only)."""
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-b:a",
        f"{AUDIO_BITRATE_BPS // 1000}k",
        str(out),
    ]


def chunk_args(audio: Path | str, plan: ChunkPlan, out: Path | str) -> list[str]:
    """Cut one chunk, re-encoded so start and length are sample-accurate.

    `-c copy` on MP3 snaps to frame boundaries and the bit reservoir, and CI measured a 4.005 s cut at
    3.778 s. Merged transcript timestamps are chunk start + local time, so the cut must be exact.
    """
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{plan.start_s:.3f}",
        "-i",
        str(audio),
        "-t",
        f"{plan.duration_s:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-b:a",
        f"{AUDIO_BITRATE_BPS // 1000}k",
        str(out),
    ]


def extract_audio(src: Path, out: Path, runner: media.Runner = media.run) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    runner(extract_audio_args(src, out))
    return out


def cut_chunks(audio: Path, plans: list[ChunkPlan], out_dir: Path, runner: media.Runner = media.run) -> list[Path]:
    """One file per plan. A single plan covering the whole file reuses `audio` as is."""
    if len(plans) == 1 and plans[0].start_s == 0:
        return [audio]
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for plan in plans:
        out = out_dir / f"chunk-{plan.index:03d}.mp3"
        runner(chunk_args(audio, plan, out))
        paths.append(out)
    return paths


def silence_args(audio: Path | str) -> list[str]:
    """One pass over the extracted audio: silencedetect, printed to stdout (`ametadata`)."""
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(audio),
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S},ametadata=mode=print:file=-",
        "-f", "null", "-",
    ]  # fmt: skip


def parse_silences(stdout: str) -> list[Silence]:
    """Pure: (start, end) pairs from `silence_args` output (an open silence at the end of the file is dropped)."""
    out: list[Silence] = []
    start: float | None = None
    for kind, value in _SILENCE_RE.findall(stdout):
        t = max(float(value), 0.0)
        if kind == "start":
            start = t
        elif start is not None:
            if t > start:
                out.append((start, t))
            start = None
    return out


def silences(audio: Path, runner: media.Runner = media.run) -> list[Silence]:
    """Silences of the extracted audio; [] if the pass fails (chunks are then cut at fixed points)."""
    try:
        return parse_silences(runner(silence_args(audio)))
    except media.MediaToolError:
        return []
