"""Moment candidates over a whole source (pure): trigger + reaction pairs → clip windows → pre-rank gates.

1. `signals.all_events` finds triggers and reactions (transcript, loudness, motion).
2. Gate `trigger_reaction`: a candidate exists only where a reaction follows a trigger within
   `gates.reaction_within_s` (a reaction may lead by `gates.reaction_lead_s`). Footage with no trigger, or a trigger
   nobody reacts to, never becomes a candidate.
3. Window rules (profile `window`): start `pre_roll_s` before the trigger (snapped to a word edge inside
   [pre_roll_min_s, pre_roll_max_s]); the payoff is the chain of reactions that follow each other within
   `chain_gap_s`; end `post_roll_s` after it; at least `min_s`, at most `max_s` (hard cap).
4. Gate `dead_footage`: reject a window whose loudness spread is under `gates.min_audio_std_db` and that holds no
   motion spike (walking, driving, menus, loading). Without loudness data the gate is "not evaluated".
5. Overlapping candidates (more than half of the shorter one) keep only the stronger; the best
   `gates.max_candidates` by pre-score go to the ranker.

The cutscene / story-reveal / in-game-show rules need to know what is on screen, so the Haiku ranker judges them and
rank/moment_score.py applies them as gates right after scoring, before selection.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field

from clipforge.rank.profile import MomentProfile
from clipforge.rank.signals import Event, all_events
from clipforge.schemas import GateResult, Transcript, Word

TRANSCRIPT_TRIGGERS = frozenset({"cue", "chat"})
MAX_OVERLAP = 0.5
LINE_PAUSE_S = 0.7
LINE_MAX_WORDS = 14


@dataclass(frozen=True)
class Moment:
    moment_id: str
    source_id: int
    start_s: float
    end_s: float
    trigger: Event
    reactions: tuple[Event, ...]
    payoff_s: float
    prescore: float
    gates: tuple[GateResult, ...]
    events: tuple[Event, ...]  # every event inside the window, for the ranker prompt
    text: str  # plain transcript text of the window

    @property
    def first_reaction(self) -> Event:
        return self.reactions[0]


@dataclass
class GenerationStats:
    triggers: int = 0
    reactions: int = 0
    pairs: int = 0
    unpaired_triggers: int = 0
    rejected_dead: int = 0
    rejected_short: int = 0
    overlaps_dropped: int = 0
    kept: int = 0
    signals: list[str] = field(default_factory=list)  # which signal families were available

    def summary(self) -> str:
        return (
            f"{self.triggers} trigger(s), {self.reactions} reaction(s), {self.pairs} paired, "
            f"{self.unpaired_triggers} trigger(s) with no reaction, {self.rejected_dead} dead window(s), "
            f"{self.overlaps_dropped} overlap(s) dropped, {self.kept} sent to the ranker "
            f"(signals: {', '.join(self.signals) or 'none'})"
        )


def moment_id(source_id: int, trigger_t: float) -> str:
    return f"{source_id}:m{round(trigger_t * 1000)}"


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


def _word_at(words: Sequence[Word], t: float) -> Word | None:
    return next((w for w in words if w.start_s < t < w.end_s), None)


def window_for(
    trigger: Event, payoff_s: float, words: Sequence[Word], profile: MomentProfile, duration_s: float
) -> tuple[float, float] | None:
    """(start, end) under the profile's window rules, or None when the source is too short for `min_s`."""
    rules = profile.window
    earliest, latest = trigger.t - rules.pre_roll_max_s, trigger.t - rules.pre_roll_min_s
    start = trigger.t - rules.pre_roll_s
    w = _word_at(words, start)
    if w is not None:
        if w.start_s >= earliest:
            start = w.start_s
        elif w.end_s <= latest:
            start = w.end_s
    start = max(start, 0.0)

    end = payoff_s + rules.post_roll_s
    w = _word_at(words, end)
    if w is not None:
        end = w.end_s
    if end - start > rules.max_s:
        end = start + rules.max_s
    if end - start < rules.min_s:
        start = max(0.0, min(start, earliest))
        end = max(end, start + rules.min_s)
        w = _word_at(words, end)
        if w is not None and w.end_s - start <= rules.max_s:
            end = w.end_s
    end = min(end, duration_s) if duration_s > 0 else end
    if end - start < rules.min_s - 1e-6:
        return None
    return round(start, 3), round(end, 3)


def prescore(trigger: Event, chain: Sequence[Event]) -> float:
    """0-1 before the paid ranker: reaction peak, how many reactions, how varied, how strong the trigger."""
    peak = max(r.strength for r in chain)
    kinds = len({r.kind for r in chain})
    score = (
        0.4 * min(peak / 4.0, 1.0)
        + 0.2 * min(len(chain) / 4.0, 1.0)
        + 0.2 * min(kinds / 3.0, 1.0)
        + 0.2 * min(trigger.strength / 4.0, 1.0)
    )
    return round(score, 6)


def dead_footage_gate(
    start: float, end: float, loudness_db: Sequence[float] | None, events: Sequence[Event], min_std_db: float
) -> GateResult:
    visual = any(e.kind == "motion" and start <= e.end_t and e.t <= end for e in events)
    if not loudness_db:
        return GateResult(
            name="dead_footage",
            passed=None,
            detail="no loudness data" + ("; motion spike present" if visual else ""),
        )
    inside = list(loudness_db[max(int(start), 0) : max(int(end) + 1, 0)])
    spread = statistics.pstdev(inside) if len(inside) > 1 else 0.0
    passed = spread >= min_std_db or visual
    detail = f"loudness spread {spread:.1f} dB (min {min_std_db:g}); motion spike {'yes' if visual else 'no'}"
    return GateResult(name="dead_footage", passed=passed, detail=detail)


def _overlap(a: Moment, b: Moment) -> float:
    inter = min(a.end_s, b.end_s) - max(a.start_s, b.start_s)
    shorter = min(a.end_s - a.start_s, b.end_s - b.start_s)
    return max(inter, 0.0) / shorter if shorter > 0 else 0.0


def _text(words: Sequence[Word], start: float, end: float) -> str:
    return " ".join(w.text.strip() for w in words if start <= (w.start_s + w.end_s) / 2 <= end).strip()


def timestamped_lines(words: Sequence[Word], start: float, end: float) -> str:
    """Transcript of [start, end] as `[t] words` lines, broken at pauses, for the ranker prompt."""
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


def generate_moments(
    transcript: Transcript,
    profile: MomentProfile,
    *,
    loudness_db: Sequence[float] | None = None,
    motion: Sequence[tuple[float, float]] | None = None,
) -> tuple[list[Moment], GenerationStats]:
    """Candidates for one source, best pre-score first (at most `gates.max_candidates`)."""
    words = sorted((w for w in transcript.words if w.text.strip()), key=lambda w: w.start_s)
    events = all_events(words, profile.lexicon, profile.signals, loudness_db=loudness_db, motion=motion)
    triggers = [e for e in events if e.role == "trigger"]
    reactions = [e for e in events if e.role == "reaction"]
    gates = profile.gates
    stats = GenerationStats(triggers=len(triggers), reactions=len(reactions))
    stats.signals = ["transcript"] + (["loudness"] if loudness_db else []) + (["motion"] if motion else [])
    duration = transcript.duration_s or (words[-1].end_s if words else 0.0)

    reaction_times = [r.t for r in reactions]
    built: list[Moment] = []
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
        first = reactions[first_i]
        limit = trig.t - profile.window.pre_roll_s + profile.window.max_s
        chain = _chain(first_i, reactions, profile.window.chain_gap_s, limit)
        payoff = max(r.end_t for r in chain)
        span = window_for(trig, payoff, words, profile, duration)
        if span is None:
            stats.rejected_short += 1
            continue
        start, end = span
        dt = first.t - trig.end_t
        pair_gate = GateResult(
            name="trigger_reaction",
            passed=True,
            detail=f"{trig.describe()} -> {first.describe()} ({dt:+.1f}s after the trigger ends)",
        )
        dead_gate = dead_footage_gate(start, end, loudness_db, events, gates.min_audio_std_db)
        if dead_gate.passed is False:
            stats.rejected_dead += 1
            continue
        inside = tuple(e for e in events if start <= e.t <= end)
        built.append(
            Moment(
                moment_id=moment_id(transcript.source_id, trig.t),
                source_id=transcript.source_id,
                start_s=start,
                end_s=end,
                trigger=trig,
                reactions=tuple(chain),
                payoff_s=payoff,
                prescore=prescore(trig, chain),
                gates=(pair_gate, dead_gate),
                events=inside,
                text=_text(words, start, end),
            )
        )

    built.sort(key=lambda m: (-m.prescore, m.start_s))
    kept: list[Moment] = []
    for m in built:
        if any(_overlap(m, k) > MAX_OVERLAP for k in kept):
            stats.overlaps_dropped += 1
            continue
        kept.append(m)
    kept = kept[: gates.max_candidates]
    stats.kept = len(kept)
    return kept, stats
