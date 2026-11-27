"""Moment ranker (moments-v2): find, score on the rubric, check keyframes, gate, select. Entry point `rank_moments`.

1. Find: the content pass reads the whole transcript (rank/content_pass.py); cheap signals add candidates
   (rank/moments.py); both are merged by overlap with `found_by` content / signals / both (rank/candidates.py).
2. Score: Haiku (forced tool `score_moments`) scores every candidate on the profile's rubric (0-10 each), in batches
   of `BATCH_SIZE` candidates (never by time slice). Each candidate carries `window.context_s` of transcript before
   and after it, its signal evidence and the content pass's guess, and the model proposes the final window.
3. Gates, in order: `brief` (off-brief or hateful drops it); `skip_rules` (a publisher-policy rule with action
   `reject` drops it, `penalize` multiplies the score); `content` (the ranker must see a clear trigger AND payoff:
   this replaced v1's "loud trigger plus reaction" requirement); `window` (a moment that cannot fit `window.max_s`
   is dropped, never truncated); `min_score` (weighted score below `gates.min_score`).
   Weighted score = 10 x sum(weight x score) x penalties, on 0-100. Signals only break ties.
4. Window (`apply_window_rules`): the model's start/end, extended to include the payoff when it runs past the end,
   trimmed to end at most `post_roll_s` after the payoff, with at most `max_lead_silence_s` of dead air at the start,
   then `min_s`..`max_s` on word edges. Decided on the global timeline; clips are cut from the full source file.
5. Vision (rank/vision.py): the best `vision.shortlist` survivors get a keyframe check; its scores replace the
   transcript scores and its findings can add skip rules or review flags, then the gates run again.
6. Overlapping picks (more than half of the shorter one) keep the better; the top `pipeline.top_n_per_source` are
   returned. Copy (Sonnet) is written later, only for the clips picked across all sources (pipeline.py).
7. Review flags (model flags plus profile `lexicon.sensitive` words) never drop a candidate; the Telegram preview
   asks the operator to check the clip before approving.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from clipforge import costs
from clipforge.rank.candidates import merge_candidates
from clipforge.rank.catalogue import catalogue_values
from clipforge.rank.claude_client import ClaudeClient, RankError, strict_object
from clipforge.rank.content_pass import ContentOutcome, run_content_pass
from clipforge.rank.copy import RankResult
from clipforge.rank.moments import (
    GenerationStats,
    Moment,
    generate_moments,
    overlap,
    sorted_words,
    text_in,
    timestamped_lines,
)
from clipforge.rank.profile import MomentProfile
from clipforge.rank.prompts import (
    Fewshot,
    Prompt,
    campaign_brief,
    escape_text,
    load_prompt,
    prompt_version_tag,
    render_fewshots,
)
from clipforge.rank.signals import Event, sensitive_hits
from clipforge.rank.vision import FramesFactory, VisionOutcome, apply_vision, judge_moment
from clipforge.schemas import CampaignIn, Candidate, GateResult, MomentScore, Platform, Transcript, Word

log = logging.getLogger(__name__)

BATCH_SIZE = 8
TOOL_NAME = "score_moments"
TOOL_DESCRIPTION = "Record one score item for every candidate id in the request."
MAX_TOKENS = 6000
REASON_MAX = 200
MAX_OVERLAP = 0.5
END_SNAP_S = 0.4  # the end moves to the end of the word it falls in only if that is this close
DROP_ORDER = ("brief", "skip_rules", "content", "window", "min_score")


class _ItemBase(BaseModel):
    """The fixed fields of one `score_moments` item (validated locally: strict tools reject numeric bounds)."""

    candidate_id: str
    archetype: str
    has_trigger: bool
    has_payoff: bool
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    first_beat_s: float = Field(ge=0)
    payoff_s: float = Field(ge=0)
    brief_ok: bool
    skip_flags: list[str]
    review_flags: list[str]
    reason: str


class MomentItem(_ItemBase):
    scores: dict[str, int]


def _enum_list(ids: Sequence[str]) -> dict[str, Any]:
    items: dict[str, Any] = {"type": "string", "enum": list(ids)} if ids else {"type": "string"}
    return {"type": "array", "items": items}


def moment_tool_schema(profile: MomentProfile) -> dict[str, Any]:
    props: dict[str, Any] = {
        "candidate_id": {"type": "string", "description": "Copy the candidate id exactly."},
        "archetype": {"type": "string", "enum": profile.archetype_ids},
    }
    for c in profile.rubric:
        props[c.id] = {"type": "integer", "description": f"{c.label}, 0-10."}
    props.update(
        {
            "has_trigger": {"type": "boolean"},
            "has_payoff": {"type": "boolean"},
            "start_s": {"type": "number", "description": "Absolute seconds."},
            "end_s": {"type": "number", "description": "Absolute seconds."},
            "first_beat_s": {
                "type": "number",
                "description": "Absolute seconds: the first beat a stranger must see for the payoff to land.",
            },
            "payoff_s": {"type": "number", "description": "Absolute seconds: when the payoff ends."},
            "brief_ok": {"type": "boolean"},
            "skip_flags": _enum_list([r.id for r in profile.skip_rules]),
            "review_flags": _enum_list([r.id for r in profile.review_flags]),
            "reason": {"type": "string", "description": f"One line, at most {REASON_MAX} characters."},
        }
    )
    return strict_object({"scores": {"type": "array", "items": strict_object(props)}})


def rubric_scores(raw: Any, profile: MomentProfile) -> dict[str, int] | None:
    """The rubric scores of one raw item, each an int 0-10, or None if any is missing or out of range."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for cid in profile.rubric_ids:
        value = raw.get(cid)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 10:
            return None
        out[cid] = int(round(value))
    return out


def parse_moment_scores(
    raw: Any, requested_ids: Iterable[str], profile: MomentProfile
) -> tuple[list[MomentItem], int, int]:
    """Validated items for requested ids (first one wins). Returns (items, returned, invalid).

    Unknown archetypes become `other`; unknown flag ids are dropped; a long reason is shortened."""
    wanted = set(requested_ids)
    items = raw.get("scores") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], 0, 0
    archetypes = set(profile.archetype_ids)
    skip_ids = {r.id for r in profile.skip_rules}
    flag_ids = {r.id for r in profile.review_flags}
    valid: list[MomentItem] = []
    seen: set[str] = set()
    invalid = 0
    for item in items:
        scores = rubric_scores(item, profile)
        try:
            base = _ItemBase.model_validate({k: v for k, v in item.items() if k in _ItemBase.model_fields})
        except (ValidationError, AttributeError):
            invalid += 1
            continue
        if scores is None or base.candidate_id not in wanted or base.candidate_id in seen:
            invalid += 1
            continue
        seen.add(base.candidate_id)
        valid.append(
            MomentItem(
                **base.model_dump(),
                scores=scores,
            ).model_copy(
                update={
                    "archetype": base.archetype if base.archetype in archetypes else "other",
                    "skip_flags": list(dict.fromkeys(f for f in base.skip_flags if f in skip_ids)),
                    "review_flags": list(dict.fromkeys(f for f in base.review_flags if f in flag_ids)),
                    "reason": " ".join(base.reason.split())[:REASON_MAX],
                }
            )
        )
    return valid, len(items), invalid


# --------------------------------------------------------------------------------------------------
# Prompt pieces
# --------------------------------------------------------------------------------------------------


def render_moments(moments: Sequence[Moment], words: Sequence[Word], context_s: float) -> str:
    """Each candidate with its signals, the content pass's guess, and transcript before / inside / after it."""
    blocks = []
    for m in moments:
        signals = "\n".join(f"- {e.role}: {escape_text(e.describe())}" for e in m.events) or "- (none)"
        guess = ""
        if m.archetype_hint is not None:
            guess = f"finder's guess: [{m.archetype_hint}] {escape_text(m.reason_hint)}\n"
        before = timestamped_lines(words, max(0.0, m.start_s - context_s), m.start_s - 1e-3)
        after = timestamped_lines(words, m.end_s + 1e-3, m.end_s + context_s)
        blocks.append(
            f'<candidate id="{m.moment_id}" start_s="{m.start_s:.2f}" end_s="{m.end_s:.2f}" '
            f'beat_s="{m.beat_s:.2f}" found_by="{m.found_by}">\n'
            f"{guess}signals:\n{signals}\n"
            f"before (context):\n{escape_text(before)}\n"
            f"clip:\n{escape_text(timestamped_lines(words, m.start_s, m.end_s))}\n"
            f"after (context):\n{escape_text(after)}\n</candidate>"
        )
    return "\n".join(blocks)


def prompt_values(profile: MomentProfile, campaign: CampaignIn, fewshots: Sequence[Fewshot]) -> dict[str, object]:
    return {
        **catalogue_values(profile),
        "campaign_brief": campaign_brief(campaign),
        "context_s": f"{profile.window.context_s:g}",
        "fewshots": render_fewshots(fewshots),
    }


def _fill(template: str, values: dict[str, object]) -> str:
    """The system section uses the same `$var` placeholders as the user section."""
    return Prompt("system", "0", "", template).render_user(**values)


# --------------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------------


@dataclass
class MomentOutcome:
    items: list[MomentItem] = field(default_factory=list)
    items_returned: int = 0
    invalid_items: int = 0
    missing_ids: list[str] = field(default_factory=list)
    failed_batches: list[str] = field(default_factory=list)
    calls: int = 0


def score_moments(
    moments: Sequence[Moment],
    words: Sequence[Word],
    campaign: CampaignIn,
    profile: MomentProfile,
    *,
    client: ClaudeClient,
    fewshots: Sequence[Fewshot] = (),
    prompt: Prompt | None = None,
    model: str | None = None,
) -> MomentOutcome:
    """Score every candidate with the rank model, `BATCH_SIZE` candidates per call. A declined or truncated batch is
    skipped and reported; API errors (after retries) and spend caps propagate."""
    prompt = prompt or load_prompt("score_moments")
    model = model or client.app.models.rank
    values = prompt_values(profile, campaign, fewshots)
    schema = moment_tool_schema(profile)
    system = _fill(prompt.system, values)
    outcome = MomentOutcome()
    for i in range(0, len(moments), BATCH_SIZE):
        batch = list(moments[i : i + BATCH_SIZE])
        ids = [m.moment_id for m in batch]
        rendered = render_moments(batch, words, profile.window.context_s)
        user = prompt.render_user(**values, n_candidates=len(batch), candidates=rendered)
        outcome.calls += 1
        try:
            result = client.forced_tool(
                model=model,
                system=system,
                user=user,
                tool_name=TOOL_NAME,
                tool_description=TOOL_DESCRIPTION,
                input_schema=schema,
                max_tokens=MAX_TOKENS,
            )
        except RankError as exc:
            log.warning("moment batch %s..%s skipped: %s", ids[0], ids[-1], exc)
            outcome.failed_batches.append(f"{ids[0]}..{ids[-1]}: {exc}")
            outcome.missing_ids.extend(ids)
            continue
        valid, returned, invalid = parse_moment_scores(result.data, ids, profile)
        outcome.items.extend(valid)
        outcome.items_returned += returned
        outcome.invalid_items += invalid
        got = {v.candidate_id for v in valid}
        outcome.missing_ids.extend(x for x in ids if x not in got)
    return outcome


# --------------------------------------------------------------------------------------------------
# Window rules, gates, selection (pure)
# --------------------------------------------------------------------------------------------------


def weighted_score(scores: dict[str, int], profile: MomentProfile, penalty: float = 1.0) -> float:
    raw = sum(profile.weights[c] * scores.get(c, 0) for c in profile.rubric_ids)
    return round(min(max(raw * 10 * penalty, 0.0), 100.0), 2)


def _word_containing(words: Sequence[Word], t: float) -> Word | None:
    return next((w for w in words if w.start_s < t < w.end_s), None)


@dataclass(frozen=True)
class WindowDecision:
    start_s: float
    end_s: float
    used_model: bool
    too_long: bool
    notes: tuple[str, ...]


def trim_lead(start: float, end: float, words: Sequence[Word], events: Sequence[Event], max_lead_s: float) -> float:
    """Move `start` so at most `max_lead_s` of silence comes before the first word or visual event, and so no word is
    cut at the start."""
    w = _word_containing(words, start)
    if w is not None:
        return w.start_s
    first_word = next((x.start_s for x in words if start <= x.start_s < end), None)
    first_motion = next((e.t for e in events if e.kind == "motion" and start <= e.t < end), None)
    beats = [t for t in (first_word, first_motion) if t is not None]
    if not beats:
        return start
    first = min(beats)
    return max(start, first - max_lead_s)


def apply_window_rules(
    moment: Moment,
    item: MomentItem,
    profile: MomentProfile,
    words: Sequence[Word],
    duration_s: float,
    events: Sequence[Event] = (),
) -> WindowDecision:
    """The clip window on the global timeline (pure), in this order:

    1. The model's window when it lies inside the candidate plus its context, else the candidate window.
    2. End: exactly `post_roll_s` after the payoff (a payoff past the window extends it: never truncated), moved to
       the end of the word it falls in only when that is within END_SNAP_S (else to that word's start).
    3. Start: when the clip would run past `target_max_s`, it starts at the model's first beat (`first_beat_s`), so
       only a moment that truly needs its setup runs longer.
    4. At most `max_lead_silence_s` of dead air before the first word or visual event.
    5. `min_s` (more setup from speech before, else more tail) and the `max_s` cap. `too_long` when the moment from
       its first beat to the payoff cannot fit `max_s`: the candidate is dropped, never cut."""
    rules = profile.window
    notes: list[str] = []
    lo, hi = moment.start_s - rules.context_s - 1.0, moment.end_s + rules.context_s + 1.0
    start, end, used_model = item.start_s, item.end_s, True
    if not (lo <= start < end <= hi):
        start, end, used_model = moment.start_s, moment.end_s, False
        notes.append("model window outside the candidate's context; kept the candidate window")
    payoff = item.payoff_s if start < item.payoff_s <= hi else min(max(moment.payoff_s, start), end)
    target_end = payoff + rules.post_roll_s
    if payoff > end:
        notes.append(f"extended to the payoff at {payoff:.1f}s")
    elif end > target_end + 0.3:
        notes.append(f"ended {rules.post_roll_s:g}s after the payoff")
    end = target_end
    w = _word_containing(words, end)
    if w is not None:
        end = w.end_s if (w.end_s - end <= END_SNAP_S or w.start_s <= payoff) else w.start_s
    if duration_s > 0:
        end = min(end, duration_s)
    beat = item.first_beat_s
    if end - start > rules.target_max_s + 1e-6 and start + 0.3 < beat < payoff - 1.0:
        start = beat - 0.3
        notes.append(f"started at the first beat ({beat:.1f}s)")
    too_long = payoff - start > rules.max_s - rules.post_roll_s + 1e-6
    new_start = trim_lead(start, end, words, events, rules.max_lead_silence_s)
    if new_start > start + 0.05:
        notes.append(f"trimmed {new_start - start:.1f}s of dead air at the start")
    start = new_start
    if end - start < rules.min_s:
        # More setup when there is speech before (start on a word, never on dead air), else more tail.
        target = end - rules.min_s
        earlier = [x.start_s for x in words if target - rules.context_s <= x.start_s <= target]
        if earlier:
            start = max(0.0, earlier[-1])
        else:
            end = start + rules.min_s
            if duration_s > 0 and end > duration_s:
                end = duration_s
                start = max(0.0, end - rules.min_s)
        notes.append(f"lengthened to {rules.min_s:g}s")
    if end - start > rules.max_s and not too_long:
        start = end - rules.max_s
        w = _word_containing(words, start)
        if w is not None:
            start = w.end_s
        start = trim_lead(start, end, words, events, rules.max_lead_silence_s)
        notes.append(f"capped at {rules.max_s:g}s (kept the payoff)")
    start = max(start, 0.0)
    if end - start > rules.target_max_s + 1e-6:
        notes.append(f"over the {rules.target_max_s:g}s target")
    return WindowDecision(round(start, 3), round(end, 3), used_model, too_long, tuple(notes))


@dataclass
class Evaluated:
    """One scored candidate after the gates (score None = dropped by `dropped_by`)."""

    moment: Moment
    item: MomentItem
    score: float | None
    gates: list[GateResult]
    window: WindowDecision
    dropped_by: str | None = None
    vision: dict[str, Any] | None = None

    @property
    def span(self) -> tuple[float, float]:
        return self.window.start_s, self.window.end_s


def _review_flags(item: MomentItem, text: str, profile: MomentProfile) -> list[str]:
    flags = list(item.review_flags)
    words = sensitive_hits(text, profile.lexicon.sensitive)
    if words:
        flags.append("sensitive words: " + ", ".join(words))
    return flags


def evaluate(
    moment: Moment,
    item: MomentItem,
    profile: MomentProfile,
    *,
    words: Sequence[Word],
    duration_s: float,
    events: Sequence[Event] = (),
    vision: dict[str, Any] | None = None,
) -> Evaluated:
    """Run the gates (brief, skip_rules, content, window, min_score) and the window rules for one candidate."""
    gates = list(moment.gates)
    window = apply_window_rules(moment, item, profile, words, duration_s, events)

    def done(score: float | None, dropped: str | None) -> Evaluated:
        return Evaluated(moment, item, score, gates, window, dropped, vision)

    gates.append(GateResult(name="brief", passed=item.brief_ok, detail="" if item.brief_ok else "brief_ok=false"))
    if not item.brief_ok:
        return done(None, "brief")
    rejecting = [f for f in item.skip_flags if (r := profile.skip_rule(f)) is not None and r.action == "reject"]
    penalty = 1.0
    for f in item.skip_flags:
        rule = profile.skip_rule(f)
        if rule is not None and rule.action == "penalize":
            penalty *= rule.penalty
    detail = ", ".join(item.skip_flags) if item.skip_flags else "none apply"
    if penalty < 1.0:
        detail += f" (score x{penalty:g})"
    gates.append(GateResult(name="skip_rules", passed=not rejecting, detail=detail))
    if rejecting:
        return done(None, "skip_rules")
    content_ok = item.has_trigger and item.has_payoff
    missing = [n for n, ok in (("trigger", item.has_trigger), ("payoff", item.has_payoff)) if not ok]
    gates.append(
        GateResult(
            name="content",
            passed=content_ok,
            detail="no clear " + " or ".join(missing) if missing else "trigger and payoff",
        )
    )
    if not content_ok:
        return done(None, "content")
    gates.append(
        GateResult(
            name="window",
            passed=not window.too_long,
            detail="; ".join(window.notes) or f"{window.end_s - window.start_s:.1f}s",
        )
    )
    if window.too_long:
        return done(None, "window")
    score = weighted_score(item.scores, profile, penalty)
    passed = score >= profile.gates.min_score
    gates.append(GateResult(name="min_score", passed=passed, detail=f"{score:g} (min {profile.gates.min_score:g})"))
    return done(score if passed else None, None if passed else "min_score")


def _rank_key(e: Evaluated) -> tuple[float, float, int, float]:
    return (-(e.score or 0.0), -e.moment.prescore, 0 if e.moment.found_by == "both" else 1, e.window.start_s)


def best_distinct(evaluated: Iterable[Evaluated], limit: int) -> tuple[list[Evaluated], int]:
    """The best survivors, no two sharing more than half of the shorter window. Returns (picks, overlaps dropped)."""
    picks: list[Evaluated] = []
    dropped = 0
    for e in sorted((x for x in evaluated if x.score is not None), key=_rank_key):
        if len(picks) >= limit:
            break
        if any(overlap(e.span, p.span) > MAX_OVERLAP for p in picks):
            dropped += 1
            continue
        picks.append(e)
    return picks, dropped


def to_candidate(e: Evaluated, profile: MomentProfile, words: Sequence[Word], prompt_version: str) -> Candidate:
    assert e.score is not None
    text = text_in(words, e.window.start_s, e.window.end_s)
    moment_score = MomentScore(
        profile=profile.name,
        weights_version=profile.weights_version,
        archetype=e.item.archetype,
        scores=dict(e.item.scores),
        weighted=e.score,
        reason=e.item.reason,
        skip_flags=e.item.skip_flags,
        review_flags=_review_flags(e.item, text, profile),
        gates=e.gates,
        found_by=e.moment.found_by,
        content_confidence=e.moment.confidence,
        vision=e.vision,
    )
    return Candidate(
        source_id=e.moment.source_id,
        start_s=e.window.start_s,
        end_s=e.window.end_s,
        score=int(round(e.score)),
        standalone=e.item.scores.get("clarity", 10) >= 5,
        brief_ok=True,
        reasons=f"[{e.item.archetype}] {e.item.reason}"[:400],
        prompt_version=prompt_version,
        moment=moment_score,
    )


# --------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------


def moment_note(candidate: Candidate, profile: MomentProfile | None = None) -> str:
    """One line for the copywriter: what the ranker thinks happens (plus the archetype's copy hint, if any)."""
    if candidate.moment is None:
        return ""
    label, hint = candidate.moment.archetype, ""
    if profile is not None:
        arch = next((a for a in profile.archetypes if a.id == candidate.moment.archetype), None)
        if arch is not None:
            label, hint = arch.label, arch.copy_hint
    note = f"{label}: {candidate.moment.reason}".strip()
    return f"{note} {hint}".strip() if hint else note


def overlaps_any(window: tuple[float, float], ranges: Sequence[tuple[float, float]]) -> bool:
    """True if `window` shares any footage with one of `ranges` (touching ends do not count)."""
    return any(window[0] < end and start < window[1] for start, end in ranges)


@dataclass
class MomentRun:
    """Everything one `rank_moments` run found, for the RankResult detail line and `cf source shadow-rank`."""

    signal: GenerationStats | None = None
    content: ContentOutcome | None = None
    merged: int = 0
    excluded_used: int = 0
    scoring: MomentOutcome | None = None
    evaluated: list[Evaluated] = field(default_factory=list)
    picks: list[Evaluated] = field(default_factory=list)
    vision: VisionOutcome = field(default_factory=VisionOutcome)
    overlaps_dropped: int = 0

    def dropped_counts(self) -> dict[str, int]:
        out = dict.fromkeys(DROP_ORDER, 0)
        for e in self.evaluated:
            if e.dropped_by is not None:
                out[e.dropped_by] = out.get(e.dropped_by, 0) + 1
        return out

    def summary(self, weights_version: str) -> str:
        found = {k: sum(1 for e in self.evaluated if e.moment.found_by == k) for k in ("content", "signals", "both")}
        dropped = ", ".join(f"{v} {k}" for k, v in self.dropped_counts().items() if v) or "none"
        parts = [
            f"moments ({weights_version}):",
            self.content.summary() if self.content else "content pass not run",
            f"signals: {self.signal.summary()}" if self.signal else "",
            f"merged {self.merged} candidate(s) ({found['content']} content, {found['signals']} signals, "
            f"{found['both']} both), {self.excluded_used} overlapping used footage",
            f"dropped by gate: {dropped}",
            self.vision.summary(),
            f"{len(self.picks)} picked",
        ]
        if self.scoring is not None and self.scoring.missing_ids:
            parts.append(f"{len(self.scoring.missing_ids)} unscored")
        return "; ".join(p for p in parts if p)


def prompt_tag(vision_on: bool) -> str:
    prompts = [load_prompt("propose_moments"), load_prompt("score_moments")]
    if vision_on:
        prompts.append(load_prompt("judge_frames"))
    prompts.append(load_prompt("clip_copy"))
    return prompt_version_tag(*prompts)


def rank_moments(
    transcript: Transcript,
    campaign: CampaignIn,
    platforms: Sequence[Platform],
    profile: MomentProfile,
    *,
    client: ClaudeClient,
    fewshots: Sequence[Fewshot] = (),
    loudness_db: Sequence[float] | None = None,
    motion: Sequence[tuple[float, float]] | None = None,
    exclude: Sequence[tuple[float, float]] = (),
    frames: FramesFactory | None = None,
    top_n: int | None = None,
) -> RankResult:
    """Find (content pass + signals) → score on the rubric → gates → keyframe check on the shortlist → top N, for one
    source. Candidates come back without copy (the pipeline writes it for the clips it picks across sources).
    Persists nothing except cost rows."""
    vision_on = profile.vision.enabled and frames is not None and profile.vision.shortlist > 0
    version = prompt_tag(vision_on)
    run = MomentRun()
    result = RankResult(prompt_version=version)
    result.moment_run = run
    words = sorted_words(transcript)
    duration = transcript.duration_s or (words[-1].end_s if words else 0.0)

    signal = generate_moments(transcript, profile, loudness_db=loudness_db, motion=motion)
    run.signal = signal.stats
    with costs.stage("content"):
        content = run_content_pass(transcript, campaign, profile, client=client)
    run.content = content
    result.failed_batches.extend(f"content {x}" for x in content.failed_segments)
    merged = merge_candidates(
        content.moments,
        signal.moments,
        source_id=transcript.source_id,
        words=words,
        events=signal.events,
        profile=profile,
        duration_s=duration,
    )
    if exclude:  # windows that overlap footage already used by an approved/published clip are never proposed
        kept = [m for m in merged if not overlaps_any((m.start_s, m.end_s), exclude)]
        run.excluded_used = len(merged) - len(kept)
        merged = kept
    run.merged = len(merged)
    result.windows_built = len(merged)
    if not merged:
        result.detail_extra = run.summary(profile.weights_version)
        return result

    with costs.stage("score"):
        outcome = score_moments(merged, words, campaign, profile, client=client, fewshots=fewshots)
    run.scoring = outcome
    result.failed_batches.extend(outcome.failed_batches)
    by_id = {m.moment_id: m for m in merged}
    evaluated = [
        evaluate(by_id[it.candidate_id], it, profile, words=words, duration_s=duration, events=signal.events)
        for it in outcome.items
        if it.candidate_id in by_id
    ]

    limit = top_n or client.app.pipeline.top_n_per_source
    if vision_on:
        assert frames is not None
        shortlist, _ = best_distinct(evaluated, profile.vision.shortlist)
        frames_fn = frames([e.span for e in shortlist]) if shortlist else None
        if shortlist and frames_fn is None:
            run.vision.failed.append("source media not readable; keyframe check skipped")
        checked: set[str] = set()
        budget = 2 * profile.vision.shortlist  # the shortlist, plus candidates that move up when checked ones drop
        todo = list(shortlist) if frames_fn is not None else []
        while todo and len(checked) < budget:
            for e in todo:
                if len(checked) >= budget:
                    break
                checked.add(e.moment.moment_id)
                with costs.stage("vision"):
                    judged = judge_moment(e, words, campaign, profile, client=client, frames=frames_fn,
                                          outcome=run.vision)  # fmt: skip
                if judged is None:
                    continue
                item, record = apply_vision(e.item, judged, profile)
                redone = evaluate(
                    e.moment, item, profile, words=words, duration_s=duration, events=signal.events, vision=record
                )
                evaluated[evaluated.index(e)] = redone
            # every final pick gets the check: the next round covers unchecked candidates that moved up
            picks_now, _ = best_distinct(evaluated, limit)
            todo = [e for e in picks_now if e.moment.moment_id not in checked]

    run.evaluated = evaluated
    picks, run.overlaps_dropped = best_distinct(evaluated, limit)
    run.picks = picks
    result.candidates = [to_candidate(e, profile, words, version) for e in picks]
    result.detail_extra = run.summary(profile.weights_version)
    return result


__all__ = [
    "BATCH_SIZE",
    "TOOL_NAME",
    "Evaluated",
    "MomentItem",
    "MomentOutcome",
    "MomentRun",
    "WindowDecision",
    "apply_window_rules",
    "best_distinct",
    "evaluate",
    "moment_note",
    "moment_tool_schema",
    "overlaps_any",
    "parse_moment_scores",
    "rank_moments",
    "render_moments",
    "score_moments",
    "to_candidate",
    "trim_lead",
    "weighted_score",
]
