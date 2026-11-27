"""Merge content-pass proposals with signal candidates (pure), on the source's global timeline.

A content proposal and a signal candidate that share more than half of the shorter window are one candidate,
`found_by="both"`: the content window (chosen for what happens) is kept and the signal evidence is attached. Content
proposals alone are `content` (their window still lists every signal event inside it as evidence), signal candidates
alone are `signals`. Signals only add evidence and break ties; they never gate.

Order (for the `gates.max_candidates` cap): content confidence plus half the signal pre-score, so a confident content
proposal beats a loud moment nobody can explain, and agreement between both finders ranks first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from clipforge.rank.content_pass import ContentMoment
from clipforge.rank.moments import (
    MAX_OVERLAP,
    Moment,
    events_in,
    fit_window,
    moment_id,
    overlap,
    text_in,
)
from clipforge.rank.profile import MomentProfile
from clipforge.rank.signals import Event
from clipforge.schemas import GateResult, Word

SIGNAL_WEIGHT = 0.5


def priority(m: Moment) -> float:
    return (m.confidence or 0.0) + SIGNAL_WEIGHT * m.prescore


def _payoff(c: ContentMoment, match: Moment | None, end: float) -> float:
    """The later of the content payoff and the matched signal payoff (when that lies inside the window)."""
    if match is not None and match.payoff_s <= end:
        return max(c.payoff_s, match.payoff_s)
    return c.payoff_s


def _content_moment(
    c: ContentMoment,
    match: Moment | None,
    *,
    source_id: int,
    words: Sequence[Word],
    events: Sequence[Event],
    profile: MomentProfile,
    duration_s: float,
) -> Moment | None:
    span = fit_window(c.start_s, c.end_s, words, profile.window, duration_s)
    if span is None:
        return None
    start, end = span
    proposal = GateResult(
        name="content_proposal",
        passed=None,
        detail=f"[{c.archetype}] confidence {c.confidence:.2f}" + (" (segment edge)" if c.edge else ""),
    )
    evidence = match.gates if match is not None else ()
    return Moment(
        moment_id=moment_id(source_id, c.trigger_s),
        source_id=source_id,
        start_s=start,
        end_s=end,
        beat_s=c.trigger_s,
        payoff_s=_payoff(c, match, end),
        trigger=match.trigger if match is not None else None,
        reactions=match.reactions if match is not None else (),
        prescore=match.prescore if match is not None else 0.0,
        gates=(proposal, *evidence),
        events=events_in(events, start, end),
        text=text_in(words, start, end),
        found_by="both" if match is not None else "content",
        archetype_hint=c.archetype,
        reason_hint=c.reason,
        confidence=c.confidence,
        edge=c.edge,
    )


def merge_candidates(
    content: Sequence[ContentMoment],
    signal: Sequence[Moment],
    *,
    source_id: int,
    words: Sequence[Word],
    events: Sequence[Event],
    profile: MomentProfile,
    duration_s: float,
) -> list[Moment]:
    """One list of candidates, best `priority` first, at most `gates.max_candidates`, no two sharing more than half."""
    used: set[int] = set()
    merged: list[Moment] = []
    for c in content:
        best: tuple[float, int] | None = None
        for i, s in enumerate(signal):
            if i in used:
                continue
            share = overlap((c.start_s, c.end_s), (s.start_s, s.end_s))
            if share > MAX_OVERLAP and (best is None or share > best[0]):
                best = (share, i)
        match = signal[best[1]] if best is not None else None
        if best is not None:
            used.add(best[1])
        m = _content_moment(
            c, match, source_id=source_id, words=words, events=events, profile=profile, duration_s=duration_s
        )
        if m is not None:
            merged.append(m)
    merged.extend(s for i, s in enumerate(signal) if i not in used)
    merged.sort(key=lambda m: (-priority(m), m.start_s))
    kept: list[Moment] = []
    seen_ids: set[str] = set()
    for m in merged:
        if any(overlap((m.start_s, m.end_s), (k.start_s, k.end_s)) > MAX_OVERLAP for k in kept):
            continue
        if m.moment_id in seen_ids:
            m = replace(m, moment_id=f"{m.moment_id}-{len(kept)}")
        seen_ids.add(m.moment_id)
        kept.append(m)
    return kept[: profile.gates.max_candidates]
