"""Chunk planning and merging for long audio (ARCHITECTURE §4.2, PRD F3). Pure functions, no I/O.

Audio is mono 16 kHz MP3 at 32 kbps (~14 MB per hour). Groq accepts at most 25 MB per request on the free tier
(https://console.groq.com/docs/speech-to-text), so each chunk stays under 24 MB with a safety margin for MP3 framing.

moments-v2 (whole VODs, chunk-boundary safety):
- Chunks are about `CHUNK_TARGET_S` long (never over the size limit) and overlap by `OVERLAP_S` (45 s), so a word or
  a short sentence is always heard whole by at least one chunk.
- With the silences of the audio (one ffmpeg `silencedetect` pass, `audio.silences`), each cut is moved to the
  middle of the nearest silence within `SNAP_S` of its planned point, and the seam between two chunks (where the
  merge switches from the earlier chunk's words to the later chunk's) is the middle of a silence inside the overlap.
  Without a usable silence the seam is the middle of the overlap.

Merging: each chunk's timestamps are shifted by its start onto one global timeline. Words (and segments) starting
before a seam come from the earlier chunk, the rest from the later chunk, so a word in the overlap is kept once; exact
duplicates left near a seam (same text, starts within `DUP_TOLERANCE_S`) are dropped. Everything downstream (signals,
the content pass, candidate windows, clip cuts) runs on this global timeline and the full source file, never on a
chunk.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from clipforge.schemas import Segment, Word

AUDIO_BITRATE_BPS = 32_000
MAX_CHUNK_BYTES = 24 * 1024 * 1024
CHUNK_TARGET_S = 1800.0  # 30 min: a failed request repeats little, and each request finishes well inside its timeout
OVERLAP_S = 45.0
SNAP_S = 20.0  # how far a cut may move to land in a silence
MIN_SILENCE_S = 0.3
SAFETY = 0.9  # MP3 headers/frames and bitrate wobble: ~94 min per chunk at 32 kbps
DUP_TOLERANCE_S = 0.3

Silence = tuple[float, float]


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    start_s: float
    end_s: float
    seam_before_s: float | None = None  # global time from which this chunk's words are used (None: from its start)
    seam_after_s: float | None = None  # global time before which this chunk's words are used (None: to its end)

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


@dataclass
class ChunkResult:
    """Transcription of one chunk, timestamps relative to the chunk start."""

    plan: ChunkPlan
    words: list[Word] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)


def max_chunk_seconds(
    bitrate_bps: int = AUDIO_BITRATE_BPS, max_bytes: int = MAX_CHUNK_BYTES, safety: float = SAFETY
) -> float:
    return math.floor(max_bytes * safety / (bitrate_bps / 8))


def _silence_near(t: float, silences: Sequence[Silence], lo: float, hi: float) -> float | None:
    """Middle of the silence (at least MIN_SILENCE_S long) nearest to `t` whose middle lies in [lo, hi]."""
    best: float | None = None
    for a, b in silences:
        if b - a < MIN_SILENCE_S:
            continue
        mid = (a + b) / 2
        if lo <= mid <= hi and (best is None or abs(mid - t) < abs(best - t)):
            best = mid
    return best


def plan_chunks(
    duration_s: float,
    *,
    bitrate_bps: int = AUDIO_BITRATE_BPS,
    max_bytes: int = MAX_CHUNK_BYTES,
    overlap_s: float = OVERLAP_S,
    safety: float = SAFETY,
    target_s: float = CHUNK_TARGET_S,
    silences: Sequence[Silence] = (),
) -> list[ChunkPlan]:
    """Cover [0, duration_s] with chunks of about `target_s` (never over the size limit) overlapping by about
    `overlap_s`, cut and seamed at silences where possible (see module docstring)."""
    if duration_s <= 0:
        return []
    max_s = max_chunk_seconds(bitrate_bps, max_bytes, safety)
    if max_s <= 2 * overlap_s:
        raise ValueError("chunk size limit too small for the overlap")
    length = min(target_s, max_s)
    spans: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = start + length
        if duration_s - start <= min(length * 1.5, max_s):  # the rest fits one chunk: no tiny tail chunk
            spans.append((start, duration_s))
            break
        snapped = _silence_near(
            end, silences, max(end - SNAP_S, start + 2 * overlap_s), min(end + SNAP_S, start + max_s)
        )
        end = snapped if snapped is not None else end
        spans.append((start, end))
        nxt = end - overlap_s
        snapped = _silence_near(nxt, silences, nxt - SNAP_S / 2, nxt)  # only earlier: the overlap never shrinks
        start = snapped if snapped is not None else nxt
    plans: list[ChunkPlan] = []
    seams: list[float] = []
    for (_a, b), (c, _d) in zip(spans, spans[1:], strict=False):  # c < b: the overlap is [c, b]
        middle = (c + b) / 2
        seam = _silence_near(middle, silences, c + (b - c) * 0.2, b - (b - c) * 0.2)
        seams.append(round(seam if seam is not None else middle, 3))
    for i, (a, b) in enumerate(spans):
        plans.append(
            ChunkPlan(
                index=i,
                start_s=round(a, 3),
                end_s=b if i == len(spans) - 1 else round(b, 3),
                seam_before_s=seams[i - 1] if i > 0 else None,
                seam_after_s=seams[i] if i < len(seams) else None,
            )
        )
    return plans


def _norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


def _shift_word(w: Word, offset: float) -> Word:
    start = max(0.0, w.start_s + offset)
    return Word(text=w.text, start_s=start, end_s=max(start, w.end_s + offset), prob=w.prob)


def _shift_segment(s: Segment, offset: float) -> Segment:
    start = max(0.0, s.start_s + offset)
    return Segment(text=s.text, start_s=start, end_s=max(start, s.end_s + offset))


def merge_chunks(results: list[ChunkResult]) -> tuple[list[Word], list[Segment]]:
    """Merge per-chunk transcripts into one global timeline (see module docstring)."""
    ordered = sorted(results, key=lambda r: r.plan.start_s)
    words: list[Word] = []
    segments: list[Segment] = []
    for i, res in enumerate(ordered):
        plan = res.plan
        if plan.seam_before_s is not None:
            lo = plan.seam_before_s
        else:
            lo = 0.0 if i == 0 else (plan.start_s + ordered[i - 1].plan.end_s) / 2
        if plan.seam_after_s is not None:
            hi = plan.seam_after_s
        else:
            hi = math.inf if i == len(ordered) - 1 else (ordered[i + 1].plan.start_s + plan.end_s) / 2
        offset = plan.start_s
        for w in res.words:
            sw = _shift_word(w, offset)
            if lo <= sw.start_s < hi:
                words.append(sw)
        for s in res.segments:
            ss = _shift_segment(s, offset)
            if lo <= ss.start_s < hi:
                segments.append(ss)
    words.sort(key=lambda w: (w.start_s, w.end_s))
    segments.sort(key=lambda s: (s.start_s, s.end_s))
    return _dedupe_words(words), segments


def _dedupe_words(words: list[Word]) -> list[Word]:
    out: list[Word] = []
    for w in words:
        if out and _norm(out[-1].text) == _norm(w.text) and abs(w.start_s - out[-1].start_s) < DUP_TOLERANCE_S:
            continue
        out.append(w)
    return out
