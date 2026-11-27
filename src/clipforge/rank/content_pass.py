"""Content-first moment finding (moments-v2): the whole stitched transcript, in overlapping segments, read by Haiku.

1. `plan_segments` splits the source's global timeline into `content_pass.segment_s` stretches that overlap by
   `content_pass.overlap_s`. A moment is at most `window.max_s` long and the overlap is far longer, so every moment
   lies whole inside at least one segment.
2. Each segment's transcript (global timestamps) goes to the rank model with the profile's catalogue (as examples,
   not limits), reject list and rubric. The forced tool `propose_moments` returns moments with start, end, trigger,
   payoff, archetype (a catalogue id or `other`), confidence and a one-line reason. Quiet and deadpan moments are
   asked for explicitly; loudness plays no part.
3. A proposal is clamped to its segment. One that touches an inner segment edge (within `content_pass.edge_s`) may
   be cut there and is marked `edge`.
4. `dedupe_proposals` merges proposals of the same moment from neighbouring segments on the global timeline (more
   than half of the shorter one shared). A whole proposal wins over an edge one; otherwise the union is kept (capped
   at `window.max_s`, else the more confident window).

Persists nothing except cost rows (through the client).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from clipforge.rank.catalogue import catalogue_values
from clipforge.rank.claude_client import ClaudeClient, RankError, strict_object
from clipforge.rank.moments import overlap, sorted_words, timestamped_lines
from clipforge.rank.profile import MomentProfile
from clipforge.rank.prompts import Prompt, campaign_brief, escape_text, load_prompt
from clipforge.schemas import CampaignIn, Transcript, Word

log = logging.getLogger(__name__)

TOOL_NAME = "propose_moments"
TOOL_DESCRIPTION = "Record the moments in this stretch worth a short clip (possibly none), best first."
MAX_TOKENS = 4096
REASON_MAX = 200
MIN_WORDS = 20  # a segment with less speech than this is not sent
MIN_MOMENT_S = 3.0
MERGE_OVERLAP = 0.5


@dataclass(frozen=True)
class Segment:
    index: int
    start_s: float
    end_s: float


@dataclass(frozen=True)
class ContentMoment:
    start_s: float
    end_s: float
    trigger_s: float
    payoff_s: float
    archetype: str
    confidence: float
    reason: str
    segments: tuple[int, ...]
    edge: bool = False


class Proposal(BaseModel):
    """One item of the `propose_moments` output (validated locally: strict tools reject numeric bounds)."""

    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    trigger_s: float = Field(ge=0)
    payoff_s: float = Field(ge=0)
    archetype: str
    confidence: float
    reason: str


@dataclass
class ContentOutcome:
    moments: list[ContentMoment] = field(default_factory=list)
    segments: int = 0
    calls: int = 0
    skipped_quiet: int = 0
    proposals: int = 0
    invalid: int = 0
    failed_segments: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"content pass: {self.segments} segment(s), {self.calls} call(s), {self.skipped_quiet} without speech, "
            f"{self.proposals} proposal(s) ({self.invalid} invalid), {len(self.moments)} after dedupe"
            + (f", {len(self.failed_segments)} failed" if self.failed_segments else "")
        )


def plan_segments(duration_s: float, segment_s: float, overlap_s: float) -> list[Segment]:
    """Overlapping stretches covering [0, duration_s]; the last one ends at duration_s."""
    if duration_s <= 0:
        return []
    if duration_s <= segment_s:
        return [Segment(0, 0.0, round(duration_s, 3))]
    step = segment_s - overlap_s
    out: list[Segment] = []
    start = 0.0
    while True:
        end = min(start + segment_s, duration_s)
        out.append(Segment(len(out), round(start, 3), round(end, 3)))
        if end >= duration_s:
            return out
        start += step
        if duration_s - start <= overlap_s:  # the previous segment already covers the tail with full overlap
            return out


def proposal_schema(profile: MomentProfile) -> dict[str, Any]:
    item = strict_object(
        {
            "start_s": {"type": "number", "description": "Absolute seconds."},
            "end_s": {"type": "number", "description": "Absolute seconds."},
            "trigger_s": {"type": "number", "description": "Absolute seconds."},
            "payoff_s": {"type": "number", "description": "Absolute seconds: when the payoff ends."},
            "archetype": {"type": "string", "enum": profile.archetype_ids},
            "confidence": {"type": "number", "description": "0 to 1."},
            "reason": {"type": "string", "description": f"One line, at most {REASON_MAX} characters."},
        }
    )
    return strict_object({"moments": {"type": "array", "items": item}})


def parse_proposals(
    raw: Any, segment: Segment, profile: MomentProfile, duration_s: float
) -> tuple[list[ContentMoment], int, int]:
    """Validated proposals clamped to the segment (best first, at most `max_proposals_per_segment`).
    Returns (moments, returned, invalid)."""
    items = raw.get("moments") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], 0, 0
    cfg = profile.content_pass
    archetypes = set(profile.archetype_ids)
    out: list[ContentMoment] = []
    invalid = 0
    for item in items:
        try:
            p = Proposal.model_validate(item)
        except ValidationError:
            invalid += 1
            continue
        start = max(p.start_s, segment.start_s)
        end = min(p.end_s, segment.end_s)
        if end - start < MIN_MOMENT_S:
            invalid += 1
            continue
        edge = (segment.start_s > 0 and start - segment.start_s < cfg.edge_s) or (
            segment.end_s < duration_s and segment.end_s - end < cfg.edge_s
        )
        out.append(
            ContentMoment(
                start_s=round(start, 3),
                end_s=round(end, 3),
                trigger_s=round(min(max(p.trigger_s, start), end), 3),
                payoff_s=round(min(max(p.payoff_s, start), end), 3),
                archetype=p.archetype if p.archetype in archetypes else "other",
                confidence=round(min(max(p.confidence, 0.0), 1.0), 3),
                reason=" ".join(p.reason.split())[:REASON_MAX],
                segments=(segment.index,),
                edge=edge,
            )
        )
    out.sort(key=lambda m: -m.confidence)
    return out[: cfg.max_proposals_per_segment], len(items), invalid


def _merge(a: ContentMoment, b: ContentMoment, max_s: float) -> ContentMoment:
    """One moment from two proposals of it (`a` is the more confident)."""
    if a.edge != b.edge:
        whole = b if a.edge else a
        start, end, trig, payoff = whole.start_s, whole.end_s, whole.trigger_s, whole.payoff_s
    else:
        start, end = min(a.start_s, b.start_s), max(a.end_s, b.end_s)
        trig, payoff = a.trigger_s, max(a.payoff_s, b.payoff_s)
        if end - start > max_s:
            start, end, payoff = a.start_s, a.end_s, a.payoff_s
    return ContentMoment(
        start_s=start,
        end_s=end,
        trigger_s=min(max(trig, start), end),
        payoff_s=min(max(payoff, start), end),
        archetype=a.archetype,
        confidence=max(a.confidence, b.confidence),
        reason=a.reason,
        segments=tuple(sorted(set(a.segments) | set(b.segments))),
        edge=a.edge and b.edge,
    )


def dedupe_proposals(moments: Sequence[ContentMoment], max_s: float) -> list[ContentMoment]:
    """Merge proposals of the same moment (more than half of the shorter shared), on the global timeline."""
    kept: list[ContentMoment] = []
    for m in sorted(moments, key=lambda x: (-x.confidence, x.start_s)):
        for i, k in enumerate(kept):
            if overlap((m.start_s, m.end_s), (k.start_s, k.end_s)) > MERGE_OVERLAP:
                kept[i] = _merge(k, m, max_s)
                break
        else:
            kept.append(m)
    kept.sort(key=lambda x: x.start_s)
    return kept


def _segment_words(words: Sequence[Word], seg: Segment) -> int:
    return sum(1 for w in words if seg.start_s <= (w.start_s + w.end_s) / 2 <= seg.end_s)


def _fill(template: str, values: dict[str, object]) -> str:
    return Prompt("system", "0", "", template).render_user(**values)


def run_content_pass(
    transcript: Transcript,
    campaign: CampaignIn,
    profile: MomentProfile,
    *,
    client: ClaudeClient,
    prompt: Prompt | None = None,
    model: str | None = None,
) -> ContentOutcome:
    """Propose moments over the whole transcript. A declined or truncated segment is skipped and reported; API errors
    (after retries), SpendCapReached and SourceSpendCapReached propagate."""
    prompt = prompt or load_prompt("propose_moments")
    model = model or client.app.models.rank
    cfg = profile.content_pass
    words = sorted_words(transcript)
    duration = transcript.duration_s or (words[-1].end_s if words else 0.0)
    values: dict[str, object] = {
        **catalogue_values(profile),
        "campaign_brief": campaign_brief(campaign),
        "max_proposals": cfg.max_proposals_per_segment,
    }
    system = _fill(prompt.system, values)
    schema = proposal_schema(profile)
    outcome = ContentOutcome()
    found: list[ContentMoment] = []
    for seg in plan_segments(duration, cfg.segment_s, cfg.overlap_s):
        outcome.segments += 1
        if _segment_words(words, seg) < MIN_WORDS:
            outcome.skipped_quiet += 1
            continue
        user = prompt.render_user(
            **values,
            segment_start_s=f"{seg.start_s:.1f}",
            segment_end_s=f"{seg.end_s:.1f}",
            transcript=escape_text(timestamped_lines(words, seg.start_s, seg.end_s)),
        )
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
            log.warning("content segment %d (%.0f-%.0fs) skipped: %s", seg.index, seg.start_s, seg.end_s, exc)
            outcome.failed_segments.append(f"{seg.start_s:.0f}-{seg.end_s:.0f}s: {exc}")
            continue
        moments, returned, invalid = parse_proposals(result.data, seg, profile, duration)
        outcome.proposals += returned
        outcome.invalid += invalid
        found.extend(moments)
    outcome.moments = dedupe_proposals(found, profile.window.max_s)
    return outcome
