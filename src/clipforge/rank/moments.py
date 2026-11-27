"""Candidate moments over a whole source (pure): the cheap signal finder and the shared `Moment` type.

moments-v2: signals are evidence, never a gate. The content pass (rank/content_pass.py) reads the whole transcript and
proposes moments by what they contain; this module adds the candidates cheap signals point at, and
rank/candidates.py merges both by overlap. A candidate needs a clear trigger and payoff per the ranker
(rank/moment_score.py), not a loud sound.

Signal candidates, over the full stitched timeline (never per chunk):
1. `signals.all_events` finds triggers and reactions (transcript, loudness, motion).
2. A trigger answered by a reaction within `gates.reaction_within_s` (a reaction may lead by `gates.reaction_lead_s`)
   proposes a candidate: the payoff is the chain of reactions that follow each other within `window.chain_gap_s`.
3. A strong reaction nobody's trigger explains (a laugh burst, a loud run with speech) proposes a hint candidate
   with room for the setup before it.
4. Window: from a little before the trigger to `window.post_roll_s` after the payoff, at least `min_s` and at most
   `max_s`, on word edges. The ranker sees `window.context_s` either side and proposes the final window.
5. The loudness spread of the window is recorded as evidence (`audio_evidence`), never used to drop it.
6. Overlapping candidates (more than half of the shorter one) keep only the stronger; the best
   `gates.max_candidates` by pre-score are kept.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field

from clipforge.rank.profile import MomentProfile, WindowRules
from clipforge.rank.signals import Event, all_events
from clipforge.schemas import FoundBy, GateResult, Transcript, Word

TRANSCRIPT_TRIGGERS = frozenset({"cue", "chat"})
MAX_OVERLAP = 0.5
LINE_PAUSE_S = 0.7
LINE_MAX_WORDS = 14
SIGNAL_LEAD_S = 3.0  # a signal window starts this long before its trigger (the ranker refines it)
HINT_LEAD_S = 8.0  # a reaction-only hint leaves this much room for the setup before the reaction
# a reaction of these kinds at least this strong proposes a candidate on its own
HINT_MIN_STRENGTH = {"laugh": 2.0, "loud": 2.5, "exclaim": 2.5}


@dataclass(frozen=True)
class Moment:
    """One candidate moment on the source's global timeline."""

    moment_id: str
    source_id: int
    start_s: float
    end_s: float
    beat_s: float  # the trigger or key beat
    payoff_s: float
    trigger: Event | None
    reactions: tuple[Event, ...]
    prescore: float  # 0-1 signal evidence (0 when no signal points here)
    gates: tuple[GateResult, ...]  # evidence records (pairing, loudness spread); none of them drops a candidate
    events: tuple[Event, ...]  # every signal event inside the window, for the ranker prompt
    text: str  # plain transcript text of the window
    found_by: FoundBy = "signals"
    archetype_hint: str | None = None  # content pass
    reason_hint: str = ""  # content pass
    confidence: float | None = None  # content pass
    edge: bool = False  # proposed at a content-segment edge (may be cut there)


@dataclass
class GenerationStats:
    triggers: int = 0
    reactions: int = 0
    pairs: int = 0
    hints: int = 0
    unpaired_triggers: int = 0
    rejected_short: int = 0
    overlaps_dropped: int = 0
    kept: int = 0
    signals: list[str] = field(default_factory=list)  # which signal families were available

    def summary(self) -> str:
        return (
            f"{self.triggers} trigger(s), {self.reactions} reaction(s), {self.pairs} paired, {self.hints} "
            f"reaction-only hint(s), {self.unpaired_triggers} trigger(s) with no reaction, "
            f"{self.overlaps_dropped} overlap(s) dropped, {self.kept} signal candidate(s) "
            f"(signals: {', '.join(self.signals) or 'none'})"
        )


@dataclass
class SignalResult:
    moments: list[Moment]
    events: list[Event]
    stats: GenerationStats


def moment_id(source_id: int, t: float) -> str:
    return f"{source_id}:m{round(t * 1000)}"


def pairs_with(trigger: Event, reaction: Event, within_s: float, lead_s: float) -> bool:
    """A reaction answers a trigger when it starts within [trigger.t - lead, trigger.end_t + within].

    A transcript trigger (cue phrase, chat mention) must be answered by something after its own words, so "what is
    that" is never both the trigger and its own reaction."""
    if not trigger.t - lead_s <= reaction.t <= trigger.end_t + within_s:
        return False
    return not (trigger.kind in TRANSCRIPT_TRIGGERS and reaction.t < trigger.end_t - 0.05)


def _chain(first_i: int, reactions: Sequence[Event], gap_s: float, limit_s: float) -> list[Event]:
    """reactions[first_i] plus the reactions after it that follow each other within `gap_s`, up to `limit_s`."""
    first = reactions[first_i]
    chain = [first]
    end = first.end_t
    for r in reactions[first_i + 1 :]:
        if r.t - end > gap_s or r.end_t > limit_s:
            break
        chain.append(r)
        end = max(end, r.end_t)
    return chain


def word_at(words: Sequence[Word], t: float) -> Word | None:
    """The word that `t` falls strictly inside, if any."""
    return next((w for w in words if w.start_s < t < w.end_s), None)


def fit_window(
    start: float, end: float, words: Sequence[Word], rules: WindowRules, duration_s: float
) -> tuple[float, float] | None:
    """[start, end] on word edges (never cutting a word), at least `min_s` and at most `max_s` (keeping the end, which
    holds the payoff), inside the source. None when the source is too short for `min_s`."""
    w = word_at(words, start)
    if w is not None:
        start = w.start_s
    w = word_at(words, end)
    if w is not None:
        end = w.end_s
    start = max(start, 0.0)
    if duration_s > 0:
        end = min(end, duration_s)
    if end - start > rules.max_s:
        start = end - rules.max_s
        w = word_at(words, start)
        if w is not None:
            start = w.end_s
    if end - start < rules.min_s:
        start = max(0.0, end - rules.min_s)
        w = word_at(words, start)
        if w is not None:
            start = w.start_s
        if end - start < rules.min_s:
            end = start + rules.min_s
            if duration_s > 0:
                end = min(end, duration_s)
    if end - start < rules.min_s - 1e-6:
        return None
    return round(start, 3), round(end, 3)


def prescore(trigger: Event | None, chain: Sequence[Event]) -> float:
    """0-1 signal evidence: reaction peak, how many reactions, how varied, how strong the trigger."""
    peak = max(r.strength for r in chain)
    kinds = len({r.kind for r in chain})
    trig = trigger.strength if trigger is not None else 0.0
    score = (
        0.4 * min(peak / 4.0, 1.0)
        + 0.2 * min(len(chain) / 4.0, 1.0)
        + 0.2 * min(kinds / 3.0, 1.0)
        + 0.2 * min(trig / 4.0, 1.0)
    )
    return round(score, 6)


def audio_evidence(
    start: float, end: float, loudness_db: Sequence[float] | None, events: Sequence[Event]
) -> GateResult:
    """Loudness spread and motion spikes of a window, recorded as evidence (`passed` is always None: never a gate)."""
    visual = any(e.kind == "motion" and start <= e.end_t and e.t <= end for e in events)
    if not loudness_db:
        detail = "no loudness data" + ("; motion spike" if visual else "")
        return GateResult(name="audio_evidence", passed=None, detail=detail)
    inside = list(loudness_db[max(int(start), 0) : max(int(end) + 1, 0)])
    spread = statistics.pstdev(inside) if len(inside) > 1 else 0.0
    detail = f"loudness spread {spread:.1f} dB; motion spike {'yes' if visual else 'no'}"
    return GateResult(name="audio_evidence", passed=None, detail=detail)


def overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Shared footage as a share of the shorter range."""
    inter = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return max(inter, 0.0) / shorter if shorter > 0 else 0.0


def text_in(words: Sequence[Word], start: float, end: float) -> str:
    return " ".join(w.text.strip() for w in words if start <= (w.start_s + w.end_s) / 2 <= end).strip()


def timestamped_lines(words: Sequence[Word], start: float, end: float) -> str:
    """Transcript of [start, end] as `[t] words` lines (global seconds), broken at pauses, for the ranker prompts."""
    inside = [w for w in words if start <= (w.start_s + w.end_s) / 2 <= end and w.text.strip()]
    lines: list[str] = []
    current: list[Word] = []
    for w in inside:
        if current and (w.start_s - current[-1].end_s >= LINE_PAUSE_S or len(current) >= LINE_MAX_WORDS):
            lines.append(f"[{current[0].start_s:.1f}] " + " ".join(x.text.strip() for x in current))
            current = []
        current.append(w)
    if current:
        lines.append(f"[{current[0].start_s:.1f}] " + " ".join(x.text.strip() for x in current))
    return "\n".join(lines) or "(no speech)"


def events_in(events: Sequence[Event], start: float, end: float) -> tuple[Event, ...]:
    return tuple(e for e in events if start <= e.t <= end)


def sorted_words(transcript: Transcript) -> list[Word]:
    return sorted((w for w in transcript.words if w.text.strip()), key=lambda w: w.start_s)


def generate_moments(
    transcript: Transcript,
    profile: MomentProfile,
    *,
    loudness_db: Sequence[float] | None = None,
    motion: Sequence[tuple[float, float]] | None = None,
) -> SignalResult:
    """Signal candidates for one source, best pre-score first (at most `gates.max_candidates`), plus every event."""
    words = sorted_words(transcript)
    events = all_events(words, profile.lexicon, profile.signals, loudness_db=loudness_db, motion=motion)
    triggers = [e for e in events if e.role == "trigger"]
    reactions = [e for e in events if e.role == "reaction"]
    gates, rules = profile.gates, profile.window
    stats = GenerationStats(triggers=len(triggers), reactions=len(reactions))
    stats.signals = ["transcript"] + (["loudness"] if loudness_db else []) + (["motion"] if motion else [])
    duration = transcript.duration_s or (words[-1].end_s if words else 0.0)

    def build(trig: Event | None, chain: list[Event], start: float, detail: str) -> Moment | None:
        payoff = max(r.end_t for r in chain)
        span = fit_window(start, payoff + rules.post_roll_s, words, rules, duration)
        if span is None:
            stats.rejected_short += 1
            return None
        a, b = span
        beat = trig.t if trig is not None else chain[0].t
        evidence = (
            GateResult(name="signal_pair", passed=None, detail=detail),
            audio_evidence(a, b, loudness_db, events),
        )
        return Moment(
            moment_id=moment_id(transcript.source_id, beat),
            source_id=transcript.source_id,
            start_s=a,
            end_s=b,
            beat_s=beat,
            payoff_s=payoff,
            trigger=trig,
            reactions=tuple(chain),
            prescore=prescore(trig, chain),
            gates=evidence,
            events=events_in(events, a, b),
            text=text_in(words, a, b),
        )

    reaction_times = [r.t for r in reactions]
    built: list[Moment] = []
    explained: set[int] = set()
    for trig in triggers:
        lo = bisect_left(reaction_times, trig.t - gates.reaction_lead_s)
        first_i: int | None = None
        for i in range(lo, len(reactions)):
            if reactions[i].t > trig.end_t + gates.reaction_within_s:
                break
            if pairs_with(trig, reactions[i], gates.reaction_within_s, gates.reaction_lead_s):
                first_i = i
                break
        if first_i is None:
            stats.unpaired_triggers += 1
            continue
        stats.pairs += 1
        chain = _chain(first_i, reactions, rules.chain_gap_s, trig.t - SIGNAL_LEAD_S + rules.max_s)
        explained.update(id(r) for r in chain)
        first = chain[0]
        detail = f"{trig.describe()} -> {first.describe()} ({first.t - trig.end_t:+.1f}s after the trigger ends)"
        m = build(trig, chain, trig.t - SIGNAL_LEAD_S, detail)
        if m is not None:
            built.append(m)

    for i, r in enumerate(reactions):  # strong reactions no trigger explains: the ranker finds the setup
        floor = HINT_MIN_STRENGTH.get(r.kind)
        if id(r) in explained or floor is None or r.strength < floor:
            continue
        chain = _chain(i, reactions, rules.chain_gap_s, r.t - HINT_LEAD_S + rules.max_s)
        explained.update(id(x) for x in chain)
        m = build(None, chain, r.t - HINT_LEAD_S, f"reaction only: {r.describe()}")
        if m is not None:
            stats.hints += 1
            built.append(m)

    built.sort(key=lambda m: (-m.prescore, m.start_s))
    kept: list[Moment] = []
    for m in built:
        if any(overlap((m.start_s, m.end_s), (k.start_s, k.end_s)) > MAX_OVERLAP for k in kept):
            stats.overlaps_dropped += 1
            continue
        kept.append(m)
    kept = kept[: gates.max_candidates]
    stats.kept = len(kept)
    return SignalResult(moments=kept, events=events, stats=stats)
