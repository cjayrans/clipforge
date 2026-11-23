"""Haiku window scoring (ARCHITECTURE §4.3): batches of 10, forced tool `score_windows` → `schemas.WindowScore`."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from clipforge.rank.claude_client import ClaudeClient, RankError, nullable, strict_object
from clipforge.rank.prompts import Fewshot, Prompt, campaign_brief, load_prompt, render_fewshots, render_windows
from clipforge.schemas import CampaignIn, Window, WindowScore

log = logging.getLogger(__name__)

BATCH_SIZE = 10
TOOL_NAME = "score_windows"
TOOL_DESCRIPTION = "Record one score item for every window id in the request."
MAX_TOKENS = 4096
REASONS_MAX = 400  # schemas.WindowScore.reasons


def score_tool_schema() -> dict[str, Any]:
    """Tool input schema. Bounds live in descriptions (strict tools reject min/max); pydantic enforces them."""
    item = strict_object(
        {
            "window_id": {"type": "string", "description": "Copy the window id exactly."},
            "score": {"type": "integer", "description": "0-100."},
            "standalone": {"type": "boolean", "description": "Makes sense with no prior context."},
            "brief_ok": {"type": "boolean", "description": "Complies with the campaign brief and rules."},
            "hook_potential": {"type": "integer", "description": "0-10."},
            "trim_start_s": nullable({"type": "number", "description": "Absolute seconds, or null."}),
            "trim_end_s": nullable({"type": "number", "description": "Absolute seconds, or null."}),
            "reasons": {"type": "string", "description": "At most 300 characters."},
        }
    )
    return strict_object({"scores": {"type": "array", "items": item}})


@dataclass
class ScoreOutcome:
    scores: list[WindowScore] = field(default_factory=list)
    items_returned: int = 0
    invalid_items: int = 0
    missing_ids: list[str] = field(default_factory=list)
    failed_batches: list[str] = field(default_factory=list)
    calls: int = 0

    @property
    def validity(self) -> float:
        """Share of returned items that were valid WindowScores for a requested window."""
        return 1.0 if self.items_returned == 0 else 1 - self.invalid_items / self.items_returned


def parse_scores(raw: Any, requested_ids: Iterable[str]) -> tuple[list[WindowScore], int, int]:
    """Validate a `score_windows` tool input. Returns (valid scores, items returned, invalid items).

    Invalid: not a WindowScore, an id we did not send, or a duplicate id. `reasons` over the limit is shortened
    (cosmetic); numbers out of range make the item invalid.
    """
    wanted = set(requested_ids)
    items = raw.get("scores") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return [], 0, 0
    valid: list[WindowScore] = []
    seen: set[str] = set()
    invalid = 0
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("reasons"), str):
            item = {**item, "reasons": item["reasons"][:REASONS_MAX]}
        try:
            score = WindowScore.model_validate(item)
        except ValidationError:
            invalid += 1
            continue
        if score.window_id not in wanted or score.window_id in seen:
            invalid += 1
            continue
        seen.add(score.window_id)
        valid.append(score)
    return valid, len(items), invalid


def batches(windows: Sequence[Window], size: int = BATCH_SIZE) -> list[list[Window]]:
    return [list(windows[i : i + size]) for i in range(0, len(windows), size)]


def score_windows(
    windows: Sequence[Window],
    campaign: CampaignIn,
    *,
    client: ClaudeClient,
    fewshots: Sequence[Fewshot] = (),
    prompt: Prompt | None = None,
    model: str | None = None,
) -> ScoreOutcome:
    """Score every window with the rank model. A batch the model declines or truncates is skipped and reported;
    API errors (after retries) and SpendCapReached propagate."""
    prompt = prompt or load_prompt("score_windows")
    model = model or client.app.models.rank
    brief = campaign_brief(campaign)
    shots = render_fewshots(fewshots)
    schema = score_tool_schema()
    outcome = ScoreOutcome()

    for batch in batches(windows):
        ids = [w.window_id for w in batch]
        user = prompt.render_user(
            campaign_brief=brief, fewshots=shots, n_windows=len(batch), windows=render_windows(batch)
        )
        outcome.calls += 1
        try:
            result = client.forced_tool(
                model=model,
                system=prompt.system,
                user=user,
                tool_name=TOOL_NAME,
                tool_description=TOOL_DESCRIPTION,
                input_schema=schema,
                max_tokens=MAX_TOKENS,
            )
        except RankError as exc:
            log.warning("score batch %s..%s skipped: %s", ids[0], ids[-1], exc)
            outcome.failed_batches.append(f"{ids[0]}..{ids[-1]}: {exc}")
            outcome.missing_ids.extend(ids)
            continue
        valid, returned, invalid = parse_scores(result.data, ids)
        outcome.scores.extend(valid)
        outcome.items_returned += returned
        outcome.invalid_items += invalid
        got = {s.window_id for s in valid}
        outcome.missing_ids.extend(i for i in ids if i not in got)
    return outcome
