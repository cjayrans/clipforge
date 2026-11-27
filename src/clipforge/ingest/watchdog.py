"""Stall watchdog and per-stage ceilings for long steps (moments-v2; replaces fixed duration-scaled timeouts).

Why: on 27 Nov 2026 a shadow run was killed by a fixed 4 h ffmpeg timeout the second the Mac woke from an 11 h idle
sleep. The clock had jumped while the Docker VM was paused, so a healthy process "timed out". Here time is
**active time**: a background thread ticks every `TICK_S` and adds at most `MAX_TICK_GAP_S` per tick, so a host
sleep or VM pause adds almost nothing. Progress is measured, not guessed:

- `run_watched(args)` (every ffmpeg call through `ingest.media.run`): ffmpeg writes `-progress` key=value lines to a
  file every half second, and stdout goes to a file. If neither grows for `STALL_S` (10 min) of active time, the
  process is killed and the step retried (at most `STALL_RETRIES` = 2 retries); after that `StepStalled` is raised.
- `stage(name, ceiling_s)`: a context for one pipeline stage. Every watched step, Twitch download progress hook,
  Groq chunk and Claude call checks it (`check()`), and `StageTimeout` is raised once the stage has used
  `ceiling_s` of active time. A running ffmpeg is killed at the ceiling, not after it.

Both errors are `WatchdogError`. The pipeline marks the source FAILED with an alert, and the night goes on.
Ceilings come from the first full-VOD run (source 2, 4.77 h: download 13 min, transcribe 3.8 min, signals 5.7 min,
rank 4.4 min, whole run 27 min), at about 3x (`CEILINGS`).
"""

from __future__ import annotations

import contextvars
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

TICK_S = 1.0
MAX_TICK_GAP_S = 5.0  # a longer gap between ticks is a host sleep / VM pause: it counts as one tick
POLL_S = 2.0
STALL_S = 600.0
STALL_RETRIES = 2
DEFAULT_STEP_CEILING_S = 2 * 3600.0  # a watched step outside any stage

# Active-time ceilings per stage, about 3x the first full-VOD run (4.77 h VOD).
CEILINGS: dict[str, float] = {
    "download": 40 * 60.0,  # 13.4 min
    "transcribe": 15 * 60.0,  # 3.8 min
    "rank": 35 * 60.0,  # signals 5.7 min + content pass, scoring and vision 4.4 min
    "copy": 10 * 60.0,
    "source": 90 * 60.0,  # whole source, 27 min
}


class WatchdogError(RuntimeError):
    """A step stopped making progress, or a stage used its whole active-time ceiling."""


class StepStalled(WatchdogError):
    pass


class StageTimeout(WatchdogError):
    pass


class ActiveClock:
    """Seconds of active time (host sleep and VM pauses excluded), from a daemon ticker thread."""

    def __init__(self, tick_s: float = TICK_S, max_gap_s: float = MAX_TICK_GAP_S) -> None:
        self._tick_s = tick_s
        self._max_gap_s = max_gap_s
        self._active = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def account(self, gap: float) -> None:
        """Add one tick's gap: a gap longer than `max_gap_s` (host sleep, VM pause) counts as one tick."""
        with self._lock:
            self._active += gap if gap <= self._max_gap_s else self._tick_s

    def _run(self) -> None:
        last = time.monotonic()
        while True:
            time.sleep(self._tick_s)
            now = time.monotonic()
            self.account(now - last)
            last = now

    def now(self) -> float:
        if self._thread is None:
            with self._lock:
                if self._thread is None:
                    self._thread = threading.Thread(target=self._run, name="active-clock", daemon=True)
                    self._thread.start()
        with self._lock:
            return self._active


CLOCK = ActiveClock()


@dataclass
class Stage:
    name: str
    ceiling_s: float
    started: float

    def used(self) -> float:
        return CLOCK.now() - self.started

    def remaining(self) -> float:
        return self.ceiling_s - self.used()


_STAGES: contextvars.ContextVar[tuple[Stage, ...]] = contextvars.ContextVar("watchdog_stages", default=())


@contextmanager
def stage(name: str, ceiling_s: float | None = None) -> Iterator[Stage]:
    """Run a stage under an active-time ceiling (default `CEILINGS[name]`). Stages nest; the tightest one counts."""
    ceiling = ceiling_s if ceiling_s is not None else CEILINGS.get(name, DEFAULT_STEP_CEILING_S)
    st = Stage(name, ceiling, CLOCK.now())
    token = _STAGES.set((*_STAGES.get(), st))
    try:
        yield st
    finally:
        _STAGES.reset(token)


def remaining() -> tuple[float, Stage | None]:
    """Active seconds left before the tightest enclosing stage's ceiling (DEFAULT_STEP_CEILING_S outside one)."""
    stages = _STAGES.get()
    if not stages:
        return DEFAULT_STEP_CEILING_S, None
    tightest = min(stages, key=lambda s: s.remaining())
    return tightest.remaining(), tightest


def check() -> None:
    """Raise StageTimeout if an enclosing stage has used its ceiling."""
    left, st = remaining()
    if st is not None and left <= 0:
        raise StageTimeout(f"stage {st.name} used its {st.ceiling_s / 60:.0f} min ceiling of active time")


# --------------------------------------------------------------------------------------------------
# Watched subprocesses
# --------------------------------------------------------------------------------------------------


def _with_progress(args: Sequence[str], progress_file: str) -> list[str]:
    """ffmpeg reports progress to a file every 0.5 s: `-progress <file> -stats_period 0.5` right after `ffmpeg`."""
    out = list(args)
    if out and Path(out[0]).name == "ffmpeg":
        out[1:1] = ["-progress", progress_file, "-stats_period", "0.5"]
    return out


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _kill(proc: subprocess.Popen[bytes]) -> None:
    proc.kill()
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=30)


def run_watched(
    args: Sequence[str], *, stall_s: float = STALL_S, retries: int = STALL_RETRIES, poll_s: float = POLL_S
) -> tuple[int, str, str]:
    """Run a command; kill and retry it when its progress file, stdout and the output path (last argument) stop
    growing for `stall_s` of active time, and kill it at the enclosing stage's ceiling (StageTimeout). Returns
    (returncode, stdout, stderr tail) of the attempt that finished. Raises StepStalled after `retries` retries."""
    label = Path(args[0]).name if args else "command"
    for attempt in range(retries + 1):
        check()
        with tempfile.TemporaryDirectory(prefix="watch-") as tmp:
            prog, out, err = (os.path.join(tmp, n) for n in ("progress", "stdout", "stderr"))
            cmd = _with_progress(args, prog)
            target = str(args[-1]) if args and args[-1] not in ("-", "pipe:1") else ""
            with open(out, "wb") as fo, open(err, "wb") as fe:
                proc = subprocess.Popen(cmd, stdout=fo, stderr=fe, stdin=subprocess.DEVNULL)
                last_marker = -1
                step_started = last_progress = CLOCK.now()
                stalled = False
                while proc.poll() is None:
                    time.sleep(poll_s)
                    marker = _size(prog) + _size(out) + (_size(target) if target else 0)
                    now = CLOCK.now()
                    if marker != last_marker:
                        last_marker, last_progress = marker, now
                    elif now - last_progress >= stall_s:
                        stalled = True
                        _kill(proc)
                        break
                    left, st = remaining()
                    if st is not None and left <= 0:
                        _kill(proc)
                        raise StageTimeout(
                            f"{label} killed: stage {st.name} used its {st.ceiling_s / 60:.0f} min ceiling"
                        )
                    if st is None and now - step_started >= DEFAULT_STEP_CEILING_S:
                        _kill(proc)
                        raise StageTimeout(
                            f"{label} killed after {DEFAULT_STEP_CEILING_S / 60:.0f} min outside a stage"
                        )
            if stalled:
                if attempt < retries:
                    continue
                raise StepStalled(f"{label} made no progress for {stall_s / 60:.0f} min, {retries + 1} attempts")
            with open(out, encoding="utf-8", errors="replace") as fo:
                stdout = fo.read()
            with open(err, encoding="utf-8", errors="replace") as fe:
                tail = " | ".join(fe.read().strip().splitlines()[-3:])
            return proc.returncode or 0, stdout, tail
    raise StepStalled(f"{label}: no attempt finished")  # pragma: no cover
