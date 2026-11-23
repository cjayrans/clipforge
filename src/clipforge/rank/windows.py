"""Candidate windows (pure): sentence units → 20–75 s windows → pre-score → top N.

ARCHITECTURE.md §4.3 / PRD F4. No I/O; everything here is deterministic for a given transcript.

1. `sentence_units` groups words into sentences (a word ending in . ? ! …), also breaking at long pauses. A
   transcript with no punctuation at all falls back to Whisper segment ends. No unit is longer than `max_unit_s`.
2. `build_windows` starts windows at unit starts at least `stride_s` apart, and for each start picks the unit ends
   whose window length is nearest each of `TARGET_LENGTHS_S` (only lengths inside [min_s, max_s]).
3. `prescore` ranks windows before the paid Haiku call, from speech density, question→answer patterns and (when
   the caller has it) per-second audio energy. The top `keep` windows are returned, best first.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.schemas import Transcript, Window, Word

SENTENCE_END = (".", "?", "!", "…")
CLOSING_CHARS = "\"')]”’"
PAUSE_BREAK_S = 1.5  # a silence this long ends a unit even without punctuation
TARGET_LENGTHS_S: tuple[float, ...] = (30.0, 45.0, 60.0)

# Pre-score weights. Without energy data, the energy weight is spread over the other two.
W_DENSITY = 0.5
W_QA = 0.3
W_ENERGY = 0.2
DENSITY_CAP_WPS = 3.0  # ~180 wpm counts as maximally dense
ENERGY_TOP_SECONDS = 3  # mean of the loudest N seconds inside the window

WINDOW_MIN_S = 20.0  # bounds enforced by schemas.Window
WINDOW_MAX_S = 75.0
_EPS = 1e-9


@dataclass(frozen=True)
class Unit:
    """One sentence (or pause-delimited phrase) of the transcript."""

    start_s: float
    end_s: float
    text: str
    n_words: int

    @property
    def is_question(self) -> bool:
        return self.text.rstrip(CLOSING_CHARS).endswith("?")


def _ends_sentence(text: str) -> bool:
    return text.strip().rstrip(CLOSING_CHARS).endswith(SENTENCE_END)


def _clean_words(words: Sequence[Word]) -> list[Word]:
    out = [w for w in words if w.text.strip()]
    out.sort(key=lambda w: (w.start_s, w.end_s))
    return [w if w.end_s >= w.start_s else w.model_copy(update={"end_s": w.start_s}) for w in out]


def _make_unit(words: Sequence[Word]) -> Unit:
    return Unit(
        start_s=words[0].start_s,
        end_s=max(w.end_s for w in words),
        text=" ".join(w.text.strip() for w in words),
        n_words=len(words),
    )


def sentence_units(transcript: Transcript, *, max_unit_s: float = WINDOW_MAX_S) -> list[Unit]:
    """Split the transcript into sentence units, in time order. Uses segments when there are no word timestamps."""
    words = _clean_words(transcript.words)
    if not words:
        return [
            Unit(s.start_s, max(s.end_s, s.start_s), s.text.strip(), len(s.text.split()))
            for s in sorted(transcript.segments, key=lambda s: s.start_s)
            if s.text.strip() and s.end_s - s.start_s <= max_unit_s
        ]

    punctuated = any(_ends_sentence(w.text) for w in words)
    segment_ends = sorted(s.end_s for s in transcript.segments)

    def at_segment_end(t: float) -> bool:
        return any(abs(t - e) <= 0.05 for e in segment_ends)

    units: list[Unit] = []
    current: list[Word] = []
    for i, word in enumerate(words):
        # Never let a unit grow past max_unit_s: close it before this word.
        if current and max(word.end_s, current[-1].end_s) - current[0].start_s > max_unit_s:
            units.append(_make_unit(current))
            current = []
        current.append(word)
        nxt = words[i + 1] if i + 1 < len(words) else None
        boundary = (
            nxt is None
            or _ends_sentence(word.text)
            or nxt.start_s - word.end_s >= PAUSE_BREAK_S
            or (not punctuated and at_segment_end(word.end_s))
        )
        if boundary:
            units.append(_make_unit(current))
            current = []
    # A single word longer than max_unit_s cannot be placed in any window.
    return [u for u in units if u.end_s - u.start_s <= max_unit_s]


def _pick_starts(units: Sequence[Unit], stride_s: float) -> list[int]:
    starts: list[int] = []
    last = -math.inf
    for i, u in enumerate(units):
        if u.start_s - last >= stride_s - _EPS:
            starts.append(i)
            last = u.start_s
    return starts


def _pick_ends(units: Sequence[Unit], i: int, min_s: float, max_s: float) -> list[int]:
    start = units[i].start_s
    valid = [j for j in range(i, len(units)) if min_s <= units[j].end_s - start <= max_s]
    picked: set[int] = set()
    for target in TARGET_LENGTHS_S:
        if valid:
            picked.add(min(valid, key=lambda j: (abs(units[j].end_s - start - target), j)))
    return sorted(picked)


def _normalized_energy(energy_per_s: Sequence[float]) -> list[float]:
    finite = [e for e in energy_per_s if math.isfinite(e)]
    if not finite:
        return [0.0] * len(energy_per_s)
    lo, hi = min(finite), max(finite)
    if hi - lo <= _EPS:
        return [0.0] * len(energy_per_s)
    return [(e - lo) / (hi - lo) if math.isfinite(e) else 0.0 for e in energy_per_s]


def prescore(units: Sequence[Unit], start_s: float, end_s: float, energy_norm: Sequence[float] | None) -> float:
    """Score in [0, 1] for the window made of `units` spanning [start_s, end_s]."""
    length = max(end_s - start_s, _EPS)
    density = min(sum(u.n_words for u in units) / length / DENSITY_CAP_WPS, 1.0)
    qa = 1.0 if any(a.is_question and not b.is_question for a, b in zip(units, units[1:], strict=False)) else 0.0
    if energy_norm is None:
        total = W_DENSITY + W_QA
        score = (W_DENSITY * density + W_QA * qa) / total
    else:
        lo, hi = int(math.floor(start_s)), int(math.ceil(end_s))
        inside = sorted(energy_norm[max(lo, 0) : max(hi, 0)], reverse=True)[:ENERGY_TOP_SECONDS]
        energy = sum(inside) / len(inside) if inside else 0.0
        score = W_DENSITY * density + W_QA * qa + W_ENERGY * energy
    return round(min(max(score, 0.0), 1.0), 6)


def window_id(source_id: int, start_s: float, end_s: float) -> str:
    return f"{source_id}:{round(start_s * 1000)}-{round(end_s * 1000)}"


def build_windows(
    transcript: Transcript,
    *,
    min_s: float = WINDOW_MIN_S,
    max_s: float = WINDOW_MAX_S,
    stride_s: float = 10.0,
    energy_per_s: Sequence[float] | None = None,
    keep: int = 60,
) -> list[Window]:
    """Candidate windows for one source, best pre-score first (at most `keep`).

    `energy_per_s[k]` is the loudness (any monotonic unit, e.g. dB) of second k of the source; optional.
    """
    if not WINDOW_MIN_S <= min_s < max_s <= WINDOW_MAX_S:
        raise ValueError(f"need {WINDOW_MIN_S} <= min_s < max_s <= {WINDOW_MAX_S}, got {min_s}, {max_s}")
    if stride_s <= 0 or keep <= 0:
        raise ValueError("stride_s and keep must be positive")

    units = sentence_units(transcript, max_unit_s=max_s)
    energy_norm = _normalized_energy(energy_per_s) if energy_per_s is not None else None

    windows: list[Window] = []
    seen: set[str] = set()
    for i in _pick_starts(units, stride_s):
        for j in _pick_ends(units, i, min_s, max_s):
            span = units[i : j + 1]
            start_s, end_s = units[i].start_s, units[j].end_s
            wid = window_id(transcript.source_id, start_s, end_s)
            if wid in seen:
                continue
            seen.add(wid)
            windows.append(
                Window(
                    window_id=wid,
                    source_id=transcript.source_id,
                    start_s=start_s,
                    end_s=end_s,
                    text=" ".join(u.text for u in span),
                    prescore=prescore(span, start_s, end_s, energy_norm),
                )
            )
    windows.sort(key=lambda w: (-w.prescore, w.start_s, w.end_s))
    return windows[:keep]
