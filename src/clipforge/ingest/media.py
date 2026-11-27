"""Small media utilities shared by ingest and transcribe: hashing, ffprobe, and an injectable subprocess runner.

The Mac has no ffmpeg; everything that shells out takes a `runner` so unit tests can fake it. Real-tool tests skip
when `shutil.which("ffprobe")` is None.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[Sequence[str]], str]
"""Run a command, return its stdout; raise MediaToolError on failure."""

HASH_CHUNK = 1 << 20


class MediaToolError(RuntimeError):
    """ffmpeg/ffprobe missing or failed."""


FFPROBE_TIMEOUT_S = 300  # ffprobe reads headers only


def run(args: Sequence[str], *, timeout: float | None = None) -> str:
    """Run a command, return its stdout; raise MediaToolError on failure.

    ffmpeg runs under the stall watchdog (ingest/watchdog.py): retried when it stops making progress for 10 min of
    active time, killed at the enclosing stage's ceiling. Host sleep does not count. Other tools (ffprobe) get a plain
    timeout (`timeout`, default FFPROBE_TIMEOUT_S)."""
    from clipforge.ingest import watchdog

    if args and Path(args[0]).name == "ffmpeg":
        try:
            code, stdout, tail = watchdog.run_watched(args)
        except FileNotFoundError as exc:
            raise MediaToolError(f"{args[0]} is not installed") from exc
        if code != 0:
            raise MediaToolError(f"{args[0]} failed (exit {code}): {tail}")
        return stdout
    limit = timeout if timeout is not None else FFPROBE_TIMEOUT_S
    try:
        proc = subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=limit)
    except FileNotFoundError as exc:
        raise MediaToolError(f"{args[0]} is not installed") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-3:]
        raise MediaToolError(f"{args[0]} failed (exit {exc.returncode}): {' | '.join(tail)}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaToolError(f"{args[0]} timed out after {limit}s") from exc
    return proc.stdout


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Probe:
    duration_s: float
    has_audio: bool
    has_video: bool


def ffprobe_args(path: Path | str) -> list[str]:
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type",
        "-of",
        "json",
        str(path),
    ]


def parse_ffprobe(stdout: str) -> Probe:
    """Pure: parse `ffprobe_args` JSON output."""
    try:
        data = json.loads(stdout)
        duration = float(data["format"]["duration"])
    except (ValueError, KeyError, TypeError) as exc:
        raise MediaToolError(f"ffprobe output has no duration: {stdout[:200]!r}") from exc
    kinds = {s.get("codec_type") for s in data.get("streams", [])}
    return Probe(duration_s=duration, has_audio="audio" in kinds, has_video="video" in kinds)


def probe(path: Path | str, runner: Runner = run) -> Probe:
    return parse_ffprobe(runner(ffprobe_args(path)))
