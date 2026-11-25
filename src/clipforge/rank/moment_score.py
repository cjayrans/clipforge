"""Haiku moment ranker (forced tool `score_moments`), post-score gates, selection, and `rank_moments`.

Per candidate the model returns an archetype, four 0-10 scores (reaction, event, setup clarity, absurdity), a
proposed window, skip flags, review flags and a one-line reason. Then, in order:

1. Gate `brief`: `brief_ok=false` (hateful, harassing or against the brief) drops the candidate.
2. Gate `skip_rules`: a profile skip rule with `action: reject` (e.g. mostly cutscene, story reveal, in-game show on
   its own) drops it; `action: penalize` multiplies the weighted score by the rule's `penalty`.
3. Weighted score = 10 x sum(weight x score) x penalties, on 0-100 (stored as `Candidate.score`).
4. Gate `min_score`: below `gates.min_score` is dropped (weak clips are not worth a review slot, PRD F17).
5. Window: the model's start/end is used only if it obeys the profile window rules (start within
   [pre_roll_min_s, pre_roll_max_s] before the trigger, includes the first reaction, length within [min_s, max_s]);
   otherwise the generated window stays. Both are snapped to word edges.
6. Review flags (model flags plus profile `lexicon.sensitive` words in the transcript) never drop a candidate; they
   are stored and shown on the Telegram preview so the operator checks the clip before approving.

Overlapping picks (more than half of the shorter one) keep the better; the top `pipeline.top_n_per_source` get copy.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from clipforge import compliance
from clipforge.rank.claude_client import ClaudeClient, RankError, strict_object
from clipforge.rank.copy import RankResult, write_copy
from clipforge.rank.moments import Moment, generate_moments, timestamped_lines
from clipforge.rank.profile import MomentProfile, Rule
from clipforge.rank.prompts import (
    Fewshot,
    Prompt,
    campaign_brief,
    escape_text,
    load_prompt,
    prompt_version_tag,
    render_fewshots,
)
from clipforge.rank.select import text_between
from clipforge.rank.signals import sensitive_hits
from clipforge.schemas import CampaignIn, Candidate, GateResult, MomentScore, Platform, Transcript, Word

log = logging.getLogger(__name__)

BATCH_SIZE = 8
TOOL_NAME = "score_moments"
TOOL_DESCRIPTION = "Record one score item for every candidate id in the request."
MAX_TOKENS = 4096
REASON_MAX = 200
MAX_OVERLAP = 0.5
SNAP_S = 0.4  # how far a proposed edge may move to reach a word edge


class MomentItem(BaseModel):
    """One item of the `score_moments` tool output (validated locally: strict tools reject numeric bounds)."""

    candidate_id: str
    archetype: str
    reaction: int = Field(ge=0, le=10)
    event: int = Field(ge=0, le=10)
    setup: int = Field(ge=0, le=10)
    absurdity: int = Field(ge=0, le=10)
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    brief_ok: bool
    skip_flags: list[str]
    review_flags: list[str]
    reason: str


def _enum_list(ids: Sequence[str]) -> dict[str, Any]:
    items: dict[str, Any] = {"type": "string", "enum": list(ids)} if ids else {"type": "string"}
    return {"type": "array", "items": items}


def moment_tool_schema(profile: MomentProfile) -> dict[str, Any]:
    item = strict_object(
        {
            "candidate_id": {"type": "string", "description": "Copy the candidate id exactly."},
            "archetype": {"type": "string", "enum": profile.archetype_ids},
            "reaction": {"type": "integer", "description": "0-10."},
            "event": {"type": "integer", "description": "0-10."},
            "setup": {"type": "integer", "description": "0-10."},
            "absurdity": {"type": "integer", "description": "0-10."},
            "start_s": {"type": "number", "description": "Absolute seconds."},
            "end_s": {"type": "number", "description": "Absolute seconds."},
            "brief_ok": {"type": "boolean"},
            "skip_flags": _enum_list([r.id for r in profile.skip_rules]),
            "review_flags": _enum_list([r.id for r in profile.review_flags]),
            "reason": {"type": "string", "description": f"One line, at most {REASON_MAX} characters."},
        }
    )
    return strict_object({"scores": {"type": "array", "items": item}})


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
        try:
            parsed = MomentItem.model_validate(item)
        except ValidationError:
            invalid += 1
            continue
        if parsed.candidate_id not in wanted or parsed.candidate_id in seen:
            invalid += 1
            continue
        seen.add(parsed.candidate_id)
        valid.append(
            parsed.model_copy(
                update={
                    "archetype": parsed.archetype if parsed.archetype in archetypes else "other",
                    "skip_flags": list(dict.fromkeys(f for f in parsed.skip_flags if f in skip_ids)),
                    "review_flags": list(dict.fromkeys(f for f in parsed.review_flags if f in flag_ids)),
                    "reason": " ".join(parsed.reason.split())[:REASON_MAX],
                }
            )
        )
    return valid, len(items), invalid


# --------------------------------------------------------------------------------------------------
# Prompt pieces
# --------------------------------------------------------------------------------------------------


def render_rules(rules: Sequence[Rule]) -> str:
    return "\n".join(f"- `{r.id}` ({r.label}): {r.description}" for r in rules) or "(none)"


def render_moments(moments: Sequence[Moment], words: Sequence[Word]) -> str:
    blocks = []
    for m in moments:
        signals = "\n".join(f"- {e.role}: {escape_text(e.describe())}" for e in m.events) or "- (none)"
        blocks.append(
            f'<candidate id="{m.moment_id}" start_s="{m.start_s:.2f}" end_s="{m.end_s:.2f}" '
            f'trigger_s="{m.trigger.t:.2f}">\n'
            f"signals:\n{signals}\n"
            f"transcript:\n{escape_text(timestamped_lines(words, m.start_s, m.end_s))}\n</candidate>"
        )
    return "\n".join(blocks)


def prompt_values(profile: MomentProfile, campaign: CampaignIn, fewshots: Sequence[Fewshot]) -> dict[str, object]:
    w = profile.window
    return {
        "campaign_brief": campaign_brief(campaign),
        "ranker_notes": escape_text(profile.ranker_notes.strip()),
        "archetypes": render_rules(profile.archetypes),
        "skip_rules": render_rules(profile.skip_rules),
        "review_flags": render_rules(profile.review_flags),
        "pre_roll_min_s": f"{w.pre_roll_min_s:g}",
        "pre_roll_max_s": f"{w.pre_roll_max_s:g}",
        "min_s": f"{w.min_s:g}",
        "target_max_s": f"{w.target_max_s:g}",
        "max_s": f"{w.max_s:g}",
        "fewshots": render_fewshots(fewshots),
    }


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
    """Score every candidate with the rank model in batches. A declined or truncated batch is skipped and
    reported; API errors (after retries) and SpendCapReached propagate."""
    prompt = prompt or load_prompt("score_moments")
    model = model or client.app.models.rank
    values = prompt_values(profile, campaign, fewshots)
    schema = moment_tool_schema(profile)
    system = _fill(prompt.system, values)
    outcome = MomentOutcome()
    for i in range(0, len(moments), BATCH_SIZE):
        batch = list(moments[i : i + BATCH_SIZE])
        ids = [m.moment_id for m in batch]
        user = prompt.render_user(**values, n_candidates=len(batch), candidates=render_moments(batch, words))
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


def _fill(template: str, values: dict[str, object]) -> str:
    """The system section uses the same `$var` placeholders as the user section."""
    return Prompt("system", "0", "", template).render_user(**values)


# --------------------------------------------------------------------------------------------------
# Gates, window rules, selection (pure)
# --------------------------------------------------------------------------------------------------


def weighted_score(item: MomentItem, profile: MomentProfile, penalty: float = 1.0) -> float:
    w = profile.weights
    raw = w.reaction * item.reaction + w.event * item.event + w.setup * item.setup + w.absurdity * item.absurdity
    return round(min(max(raw * 10 * penalty, 0.0), 100.0), 2)


def _snap(t: float, words: Sequence[Word], *, edge: str) -> float:
    """Move t onto a word edge within SNAP_S when it falls inside a word."""
    for w in words:
        if w.start_s < t < w.end_s:
            if edge == "start" and t - w.start_s <= SNAP_S:
                return w.start_s
            if edge == "end" and w.end_s - t <= SNAP_S:
                return w.end_s
    return t


def apply_window_rules(
    moment: Moment, item: MomentItem, profile: MomentProfile, words: Sequence[Word], duration_s: float
) -> tuple[float, float, bool]:
    """(start, end, used_model_window). The model's window must obey the profile rules, else the generated one."""
    rules = profile.window
    trig = moment.trigger.t
    start = _snap(item.start_s, words, edge="start")
    end = _snap(item.end_s, words, edge="end")
    if duration_s > 0:
        end = min(end, duration_s)
    ok = (
        trig - rules.pre_roll_max_s - 1e-6 <= start <= trig - min(rules.pre_roll_min_s, trig) + 1e-6
        and end >= moment.first_reaction.end_t
        and rules.min_s - 1e-6 <= end - start <= rules.max_s + 1e-6
    )
    if ok:
        return round(start, 3), round(end, 3), True
    return moment.start_s, moment.end_s, False


@dataclass(frozen=True)
class MomentPick:
    candidate: Candidate
    moment: Moment
    item: MomentItem
    text: str


@dataclass
class SelectionStats:
    rejected_brief: int = 0
    rejected_skip: int = 0
    rejected_score: int = 0
    overlaps_dropped: int = 0
    model_windows_used: int = 0


def _review_flags(item: MomentItem, moment: Moment, profile: MomentProfile) -> list[str]:
    flags = list(item.review_flags)
    words = sensitive_hits(moment.text, profile.lexicon.sensitive)
    if words:
        flags.append("sensitive words: " + ", ".join(words))
    return flags


def gate_and_score(
    moment: Moment, item: MomentItem, profile: MomentProfile
) -> tuple[float | None, list[GateResult], float]:
    """(weighted score or None when a gate drops it, gate results incl. the pre-rank ones, penalty)."""
    gates = list(moment.gates)
    gates.append(GateResult(name="brief", passed=item.brief_ok, detail="" if item.brief_ok else "brief_ok=false"))
    if not item.brief_ok:
        return None, gates, 1.0
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
        return None, gates, penalty
    score = weighted_score(item, profile, penalty)
    passed = score >= profile.gates.min_score
    gates.append(GateResult(name="min_score", passed=passed, detail=f"{score:g} (min {profile.gates.min_score:g})"))
    return (score if passed else None), gates, penalty


def select_moments(
    moments: Sequence[Moment],
    items: Sequence[MomentItem],
    profile: MomentProfile,
    *,
    words: Sequence[Word],
    duration_s: float,
    top_n: int,
    prompt_version: str,
) -> tuple[list[MomentPick], SelectionStats]:
    by_id = {m.moment_id: m for m in moments}
    stats = SelectionStats()
    scored: list[tuple[float, Moment, MomentItem, list[GateResult], float, float, bool]] = []
    for item in items:
        m = by_id.get(item.candidate_id)
        if m is None:
            continue
        score, gates, _penalty = gate_and_score(m, item, profile)
        if score is None:
            failed = next(g.name for g in reversed(gates) if g.passed is False)
            if failed == "brief":
                stats.rejected_brief += 1
            elif failed == "skip_rules":
                stats.rejected_skip += 1
            else:
                stats.rejected_score += 1
            continue
        start, end, used_model = apply_window_rules(m, item, profile, words, duration_s)
        scored.append((score, m, item, gates, start, end, used_model))
    scored.sort(key=lambda t: (-t[0], -t[1].prescore, t[4]))

    picks: list[MomentPick] = []
    for score, m, item, gates, start, end, used_model in scored:
        if len(picks) >= top_n:
            break
        if any(_overlap((start, end), (p.candidate.start_s, p.candidate.end_s)) > MAX_OVERLAP for p in picks):
            stats.overlaps_dropped += 1
            continue
        stats.model_windows_used += int(used_model)
        moment_score = MomentScore(
            profile=profile.name,
            weights_version=profile.weights_version,
            archetype=item.archetype,
            reaction=item.reaction,
            event=item.event,
            setup=item.setup,
            absurdity=item.absurdity,
            weighted=score,
            reason=item.reason,
            skip_flags=item.skip_flags,
            review_flags=_review_flags(item, m, profile),
            gates=gates,
        )
        candidate = Candidate(
            source_id=m.source_id,
            start_s=start,
            end_s=end,
            score=int(round(score)),
            standalone=item.setup >= 5,
            brief_ok=True,
            reasons=f"[{item.archetype}] {item.reason}"[:400],
            prompt_version=prompt_version,
            moment=moment_score,
        )
        text = text_between(words, start, end) or m.text
        picks.append(MomentPick(candidate=candidate, moment=m, item=item, text=text))
    return picks, stats


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return max(inter, 0.0) / shorter if shorter > 0 else 0.0


# --------------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------------


def moment_note(candidate: Candidate, profile_labels: dict[str, str] | None = None) -> str:
    """One line for the copywriter: what the ranker thinks happens."""
    if candidate.moment is None:
        return ""
    label = (profile_labels or {}).get(candidate.moment.archetype, candidate.moment.archetype)
    return f"{label}: {candidate.moment.reason}".strip()


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
) -> RankResult:
    """Signals → candidates + pre-rank gates → Haiku → post gates → top N → Sonnet copy, for one source.
    Persists nothing except cost rows (the pipeline writes candidates)."""
    cfg = client.app.pipeline
    score_prompt, copy_prompt = load_prompt("score_moments"), load_prompt("clip_copy")
    version = prompt_version_tag(score_prompt, copy_prompt)
    words = sorted((w for w in transcript.words if w.text.strip()), key=lambda w: w.start_s)
    moments, gen = generate_moments(transcript, profile, loudness_db=loudness_db, motion=motion)
    result = RankResult(windows_built=len(moments), prompt_version=version)
    if not moments:
        result.detail_extra = f"moments: {gen.summary()}"
        return result
    outcome = score_moments(moments, words, campaign, profile, client=client, fewshots=fewshots, prompt=score_prompt)
    result.failed_batches = list(outcome.failed_batches)
    picks, sel = select_moments(
        moments,
        outcome.items,
        profile,
        words=words,
        duration_s=transcript.duration_s,
        top_n=cfg.top_n_per_source,
        prompt_version=version,
    )
    labels = {a.id: a.label for a in profile.archetypes}
    for pick in picks:
        try:
            copy = write_copy(
                pick.candidate,
                pick.text,
                campaign,
                platforms,
                client=client,
                fewshots=fewshots,
                prompt=copy_prompt,
                moment=moment_note(pick.candidate, labels),
            )
        except (RankError, compliance.ComplianceError) as exc:
            log.warning("copy failed for %s: %s", pick.moment.moment_id, exc)
            result.copy_failures.append(f"{pick.moment.moment_id}: {exc}")
            continue
        result.candidates.append(pick.candidate.model_copy(update={"copy_": copy}))
    result.detail_extra = (
        f"moments ({profile.weights_version}): {gen.summary()}; ranker dropped {sel.rejected_brief} off-brief, "
        f"{sel.rejected_skip} by skip rules, {sel.rejected_score} under min score; "
        f"{len(outcome.missing_ids)} unscored"
    )
    return result
