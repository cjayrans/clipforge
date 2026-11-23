"""Chunk planning and merging for long audio (ARCHITECTURE §4.2, PRD F3). Pure functions, no I/O.

Audio is mono 16 kHz MP3 at 32 kbps (~14 MB per hour). Groq accepts at most 25 MB per request on the free tier
(https://console.groq.com/docs/speech-to-text), so each chunk stays under 24 MB with a safety margin for MP3 framing,
and consecutive chunks overlap by 2 s so no word is cut in half at a boundary.

Merging: each chunk's timestamps are shifted by its start. Inside an overlap, words (and segments) starting before
the overlap's midpoint come from the earlier chunk and the rest from the later chunk; exact duplicates left near the
seam (same text, starts within `DUP_TOLERANCE_S`) are dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from clipforge.schemas import Segment, Word

AUDIO_BITRATE_BPS = 32_000
MAX_CHUNK_BYTES = 24 * 1024 * 1024
OVERLAP_S = 2.0
SAFETY = 0.9  # MP3 headers/frames and bitrate wobble: ~94 min per chunk at 32 kbps
DUP_TOLERANCE_S = 0.3


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    start_s: float
    end_s: float

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


def plan_chunks(
    duration_s: float,
    *,
    bitrate_bps: int = AUDIO_BITRATE_BPS,
    max_bytes: int = MAX_CHUNK_BYTES,
    overlap_s: float = OVERLAP_S,
    safety: float = SAFETY,
) -> list[ChunkPlan]:
    """Cover [0, duration_s] with chunks no longer than the size limit allows, overlapping by `overlap_s`."""
    if duration_s <= 0:
        return []
    max_s = max_chunk_seconds(bitrate_bps, max_bytes, safety)
    if max_s <= 2 * overlap_s:
        raise ValueError("chunk size limit too small for the overlap")
    plans: list[ChunkPlan] = []
    start = 0.0
    while True:
        end = min(start + max_s, duration_s)
        plans.append(ChunkPlan(index=len(plans), start_s=start, end_s=end))
        if end >= duration_s:
            return plans
        start = end - overlap_s


def _norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower())


def _shift_word(w: Word, offset: float) -> Word:
    start = max(0.0, w.start_s + offset)
    return Word(text=w.text, start_s=start, end_s=max(start, w.end_s + offset), prob=w.prob)


def _shift_segment(s: Segment, offset: float) -> Segment:
    start = max(0.0, s.start_s + offset)
    return Segment(text=s.text, start_s=start, end_s=max(start, s.end_s + offset))


def merge_chunks(results: list[ChunkResult]) -> tuple[list[Word], list[Segment]]:
    """Merge per-chunk transcripts into one timeline (see module docstring)."""
    ordered = sorted(results, key=lambda r: r.plan.start_s)
    words: list[Word] = []
    segments: list[Segment] = []
    for i, res in enumerate(ordered):
        lo = 0.0 if i == 0 else (res.plan.start_s + ordered[i - 1].plan.end_s) / 2
        hi = math.inf if i == len(ordered) - 1 else (ordered[i + 1].plan.start_s + res.plan.end_s) / 2
        offset = res.plan.start_s
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
