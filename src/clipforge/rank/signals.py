"""Cheap moment signals over a whole source (pure): trigger and reaction events.

Inputs are the Groq transcript (word timestamps), per-second loudness (dB) and frame-difference motion samples
(rank/media_signals.py extracts both with ffmpeg). Thresholds and word lists come from the moment profile.

Triggers (something happens): a motion spike, a loud sound with no speech (`onset`), a speech gap followed by a
reaction (`silence_beat`), a cue phrase ("look at this", "wait"), a word addressed to chat.
Reactions (the streamer responds): an exclamation, laughter, a loud second with speech.

Every event has `t` (start) and `end_t`. A reaction pairs with a trigger when
`trigger.t - reaction_lead_s <= reaction.t <= trigger.end_t + reaction_within_s` (rank/moments.py).

Not detected here (see the PR description): face-cam expression (the image ships only MediaPipe's face *detector*,
which has no expression output), HUD changes and chat velocity (no chat data in campaign footage).
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from clipforge.rank.profile import Lexicon, SignalRules
from clipforge.schemas import Word

Role = Literal["trigger", "reaction"]
TriggerKind = Literal["motion", "onset", "silence_beat", "cue", "chat"]
ReactionKind = Literal["exclaim", "laugh", "loud"]

DB_FLOOR = -90.0  # silence (-inf dB) is clamped here
MIN_SIGMA_DB = 3.0  # with loud_z 2 a spike is >= 6 dB over the median; a flat baseline makes MAD collapse
MIN_SIGMA_MOTION = 1.0
MIN_HISTORY = 8  # samples needed before a trailing baseline is trusted
BURST_MAX_WORDS = 3  # an exclamation word inside an utterance this short counts as stressed
BURST_PAUSE_S = 0.35
MOTION_MAX_SPAN_S = 2.5  # a motion spike between two far-apart keyframes is placed this close to the later one
WORD_STRENGTH = 2.0  # strength of a transcript event (z-score scale, so it is comparable with loudness)
_PUNCT = re.compile(r"[^\w' ]+")


@dataclass(frozen=True)
class Event:
    role: Role
    kind: str
    t: float
    end_t: float
    strength: float
    detail: str

    def describe(self) -> str:
        return f"{self.kind} at {self.t:.1f}s ({self.detail})"


def norm_word(text: str) -> str:
    return _PUNCT.sub("", text.lower()).strip()


# --------------------------------------------------------------------------------------------------
# z-scores
# --------------------------------------------------------------------------------------------------


def rolling_z(values: Sequence[float], times: Sequence[float], baseline_s: float, min_sigma: float) -> list[float]:
    """z-score of each sample against the samples in the trailing `baseline_s` (robust: median and MAD).

    Early samples without enough history use the first `baseline_s` of the source as their baseline."""
    if len(values) != len(times):
        raise ValueError("values and times must have the same length")
    clean = [v if math.isfinite(v) else DB_FLOOR for v in values]
    out: list[float] = []
    lo = 0
    first_window = [v for v, t in zip(clean, times, strict=True) if t - times[0] <= baseline_s] if clean else []
    for i, (v, t) in enumerate(zip(clean, times, strict=True)):
        while lo < i and times[lo] < t - baseline_s:
            lo += 1
        history = clean[lo:i]
        base = history if len(history) >= MIN_HISTORY else first_window
        med = statistics.median(base)
        mad = statistics.median(abs(x - med) for x in base)
        sigma = max(1.4826 * mad, min_sigma)
        out.append((v - med) / sigma)
    return out


# --------------------------------------------------------------------------------------------------
# Transcript events
# --------------------------------------------------------------------------------------------------


def _phrase_hits(words: Sequence[Word], norms: Sequence[str], phrases: Sequence[str]) -> list[tuple[int, int, str]]:
    """(first index, last index, phrase) for every phrase occurrence; phrases are lowercase, space-separated."""
    hits: list[tuple[int, int, str]] = []
    split = [p.split() for p in phrases]
    for i in range(len(words)):
        for parts, phrase in zip(split, phrases, strict=True):
            n = len(parts)
            if n and list(norms[i : i + n]) == parts:
                hits.append((i, i + n - 1, phrase))
    return hits


def _in_burst(words: Sequence[Word], i: int) -> bool:
    """True when word i sits in a short utterance (pauses on both sides) of at most BURST_MAX_WORDS words."""
    lo = i
    while lo > 0 and words[lo].start_s - words[lo - 1].end_s < BURST_PAUSE_S:
        lo -= 1
    hi = i
    while hi + 1 < len(words) and words[hi + 1].start_s - words[hi].end_s < BURST_PAUSE_S:
        hi += 1
    return hi - lo + 1 <= BURST_MAX_WORDS


def transcript_events(words: Sequence[Word], lexicon: Lexicon) -> list[Event]:
    """Exclamations, laughter, cue phrases and chat mentions, in time order."""
    words = sorted((w for w in words if w.text.strip()), key=lambda w: w.start_s)
    norms = [norm_word(w.text) for w in words]
    laugh = [re.compile(p) for p in lexicon.laughter_patterns]
    exclaims = set(lexicon.exclamations)
    chat = set(lexicon.chat)
    events: list[Event] = []
    used: set[int] = set()

    for first, last, phrase in _phrase_hits(words, norms, lexicon.exclamation_phrases):
        events.append(
            Event("reaction", "exclaim", words[first].start_s, words[last].end_s, WORD_STRENGTH + 0.5, phrase)
        )
        used.update(range(first, last + 1))
    for i, (w, n) in enumerate(zip(words, norms, strict=True)):
        if i in used or not n:
            continue
        raw = w.text.strip()
        if any(p.match(n) or p.match(raw.lower()) for p in laugh):
            events.append(Event("reaction", "laugh", w.start_s, w.end_s, WORD_STRENGTH, n))
            continue
        if n in exclaims:
            repeated = (i > 0 and norms[i - 1] == n) or (i + 1 < len(norms) and norms[i + 1] == n)
            stressed = raw.endswith(("!", "?")) or repeated or _in_burst(words, i)
            if stressed:
                events.append(
                    Event("reaction", "exclaim", w.start_s, w.end_s, WORD_STRENGTH + (0.5 if repeated else 0), n)
                )
        if n in chat:
            events.append(Event("trigger", "chat", w.start_s, w.end_s, 1.0, n))
    for first, last, phrase in _phrase_hits(words, norms, lexicon.cues):
        events.append(Event("trigger", "cue", words[first].start_s, words[last].end_s, 1.0, phrase))
    return _dedupe(events)


def _dedupe(events: list[Event]) -> list[Event]:
    """Drop exact-duplicate (role, kind, t) events, keeping the strongest; sort by time."""
    best: dict[tuple[str, str, float], Event] = {}
    for e in events:
        key = (e.role, e.kind, round(e.t, 2))
        if key not in best or e.strength > best[key].strength:
            best[key] = e
    return sorted(best.values(), key=lambda e: (e.t, e.role, e.kind))


def sensitive_hits(text: str, terms: Sequence[str]) -> list[str]:
    """Sensitive terms (profile lexicon) that occur in `text` as whole words or phrases."""
    norm = " " + " ".join(norm_word(w) for w in text.split()) + " "
    return [t for t in terms if f" {t} " in norm]


# --------------------------------------------------------------------------------------------------
# Loudness and motion events
# --------------------------------------------------------------------------------------------------


def _speech_at(words: Sequence[Word], start: float, end: float) -> bool:
    return any(w.start_s < end and w.end_s > start for w in words)


def loudness_events(loudness_db: Sequence[float], words: Sequence[Word], rules: SignalRules) -> list[Event]:
    """Runs of seconds at or above `loud_z`: `loud` reactions when someone speaks, else `onset` triggers.

    `loudness_db[k]` is the RMS level of second k."""
    if not loudness_db:
        return []
    times = [float(k) for k in range(len(loudness_db))]
    z = rolling_z(loudness_db, times, rules.baseline_s, MIN_SIGMA_DB)
    events: list[Event] = []
    k = 0
    while k < len(z):
        if z[k] < rules.loud_z:
            k += 1
            continue
        j = k
        while j + 1 < len(z) and z[j + 1] >= rules.loud_z:
            j += 1
        speech = [s for s in range(k, j + 1) if _speech_at(words, s, s + 1)]
        quiet = [s for s in range(k, j + 1) if s not in speech]
        if speech:
            peak = max(z[s] for s in speech)
            events.append(Event("reaction", "loud", float(speech[0]), float(speech[-1] + 1), peak, f"z {peak:.1f}"))
        if quiet and (not speech or quiet[0] < speech[0]):
            peak = max(z[s] for s in quiet)
            events.append(Event("trigger", "onset", float(quiet[0]), float(quiet[0] + 1), peak, f"z {peak:.1f}"))
        k = j + 1
    return events


def motion_events(samples: Sequence[tuple[float, float]], rules: SignalRules) -> list[Event]:
    """Motion spikes from (time, mean frame difference) samples; each spike spans from the previous sample."""
    if len(samples) < 2:
        return []
    samples = sorted(samples)
    times = [t for t, _ in samples]
    diffs = [d for _, d in samples]
    z = rolling_z(diffs, times, rules.baseline_s, MIN_SIGMA_MOTION)
    events: list[Event] = []
    for i in range(1, len(samples)):
        if z[i] >= rules.motion_z and diffs[i] >= rules.motion_min_diff:
            start = max(times[i - 1], times[i] - MOTION_MAX_SPAN_S)
            events.append(Event("trigger", "motion", start, times[i], z[i], f"z {z[i]:.1f}"))
    return events


def silence_beats(words: Sequence[Word], reactions: Sequence[Event], rules: SignalRules) -> list[Event]:
    """A speech gap of at least `silence_beat_s` that ends with a reaction within `silence_reaction_s`.

    The trigger spans the gap: whatever made the streamer go quiet happened at its start."""
    words = sorted((w for w in words if w.text.strip()), key=lambda w: w.start_s)
    events: list[Event] = []
    for prev, nxt in zip(words, words[1:], strict=False):
        gap = nxt.start_s - prev.end_s
        if gap < rules.silence_beat_s:
            continue
        if any(nxt.start_s - 0.1 <= r.t <= nxt.start_s + rules.silence_reaction_s for r in reactions):
            events.append(Event("trigger", "silence_beat", prev.end_s, nxt.start_s, 1.0 + gap, f"{gap:.1f}s quiet"))
    return events


def all_events(
    words: Sequence[Word],
    lexicon: Lexicon,
    rules: SignalRules,
    *,
    loudness_db: Sequence[float] | None = None,
    motion: Sequence[tuple[float, float]] | None = None,
) -> list[Event]:
    """Every trigger and reaction event for one source, in time order."""
    events = transcript_events(words, lexicon)
    if loudness_db:
        events += loudness_events(loudness_db, words, rules)
    if motion:
        events += motion_events(motion, rules)
    reactions = [e for e in events if e.role == "reaction"]
    events += silence_beats(words, reactions, rules)
    return sorted(events, key=lambda e: (e.t, e.role, e.kind))
