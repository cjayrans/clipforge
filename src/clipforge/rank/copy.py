"""Sonnet copywriter (ARCHITECTURE §4.3, PRD F4): hook (≤ 8 words), YouTube title (≤ 60 chars), per-platform
captions → `schemas.ClipCopy`, every caption passed through `compliance.normalize_caption` (invariants 2 and 9).

Claude Sonnet 5.5 rejects forced tool choice, so this uses structured outputs (`output_config.format`); see
claude_client.py for the verified facts. Effort is `low`: short creative output, and it keeps a month of nightly
copy (~480 calls) well inside the $15 cap.

`rank_source` chains windows → score → select → copy for one source. It persists nothing except cost rows; Phase 2
(pipeline) writes candidates, events and the source's RANKED state.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from clipforge import compliance
from clipforge.rank.claude_client import ClaudeClient, Effort, RankError, strict_object
from clipforge.rank.prompts import Fewshot, Prompt, campaign_brief, escape_text, load_prompt, prompt_version_tag
from clipforge.rank.prompts import render_fewshots as _render_fewshots
from clipforge.rank.score import ScoreOutcome, score_windows
from clipforge.rank.select import Selection, select_candidates
from clipforge.rank.windows import build_windows
from clipforge.schemas import CampaignIn, Candidate, ClipCopy, Platform, PlatformCaption, Transcript

log = logging.getLogger(__name__)

COPY_EFFORT: Effort = "low"
MAX_TOKENS = 8000
HOOK_MAX_WORDS = 8
HOOK_MAX_CHARS = 60  # schemas.ClipCopy.hook
TITLE_MAX_CHARS = 60  # schemas.ClipCopy.youtube_title


def copy_schema(platforms: Sequence[Platform]) -> dict[str, Any]:
    """Structured-output schema for ClipCopy. Length limits are in descriptions; repair_copy enforces them."""
    caption = strict_object(
        {
            "platform": {"type": "string", "enum": [p.value for p in platforms]},
            "text": {"type": "string", "description": "Caption body: no disclosure, no URLs."},
        }
    )
    return strict_object(
        {
            "hook": {"type": "string", "description": f"At most {HOOK_MAX_WORDS} words."},
            "youtube_title": {"type": "string", "description": f"At most {TITLE_MAX_CHARS} characters."},
            "captions": {"type": "array", "items": caption},
        }
    )


def _shorten(text: str, max_chars: int, max_words: int | None = None) -> str:
    words = " ".join(text.split()).split(" ")
    if max_words is not None:
        words = words[:max_words]
    out = " ".join(words).strip()
    while len(out) > max_chars and " " in out:
        out = out.rsplit(" ", 1)[0]
    return out[:max_chars].strip()


def repair_copy(raw: Any, platforms: Sequence[Platform]) -> ClipCopy:
    """Turn model JSON into a valid ClipCopy for exactly `platforms` (pure).

    Hook cut to 8 words / 60 chars, title to 60 chars, at word boundaries. One caption per platform, in `platforms`
    order; a platform the model skipped (or left empty) borrows the first non-empty caption, else the hook.
    """
    if not isinstance(raw, dict):
        raise RankError("copy output is not a JSON object")
    hook = _shorten(str(raw.get("hook") or ""), HOOK_MAX_CHARS, HOOK_MAX_WORDS)
    if not hook:
        raise RankError("copy output has an empty hook")
    title = _shorten(str(raw.get("youtube_title") or ""), TITLE_MAX_CHARS) or _shorten(hook, TITLE_MAX_CHARS)

    by_platform: dict[Platform, str] = {}
    for item in raw.get("captions") or []:
        if not isinstance(item, dict):
            continue
        try:
            platform = Platform(str(item.get("platform")))
        except ValueError:
            continue
        text = str(item.get("text") or "").strip()
        if platform in platforms and text and platform not in by_platform:
            by_platform[platform] = text
    fallback = next((by_platform[p] for p in platforms if p in by_platform), hook)
    captions = [PlatformCaption(platform=p, text=by_platform.get(p, fallback)) for p in platforms]
    return ClipCopy(hook=hook, youtube_title=title, captions=captions)


def finalize_captions(copy: ClipCopy, campaign: CampaignIn) -> ClipCopy:
    """Invariants 2 and 9: '#ad ' first, required tags appended, no URLs on X, platform length limits."""
    captions = [
        PlatformCaption(platform=c.platform, text=compliance.normalize_caption(c.text, c.platform, campaign))
        for c in copy.captions
    ]
    return copy.model_copy(update={"captions": captions})


def hook_key(hook: str) -> str:
    """Comparison key for hooks: lowercase letters and digits only ("Nobody saw THAT coming!" == "nobody saw that
    coming")."""
    return "".join(ch for ch in hook.lower() if ch.isalnum())


def repeats_hook(hook: str, used: Sequence[str]) -> bool:
    key = hook_key(hook)
    return any(key == hook_key(u) for u in used if hook_key(u))


def render_avoid_hooks(hooks: Sequence[str]) -> str:
    unique = list(dict.fromkeys(h.strip() for h in hooks if h.strip()))
    return "\n".join(f"- {escape_text(h)}" for h in unique) or "(none)"


class RepeatedHookError(RankError):
    """The model kept returning a hook this clip already had ("New hook" must never show the same hook again)."""


HOOK_ATTEMPTS = 2  # one retry when the model repeats a used hook


def write_copy(
    candidate: Candidate,
    clip_text: str,
    campaign: CampaignIn,
    platforms: Sequence[Platform],
    *,
    client: ClaudeClient,
    fewshots: Sequence[Fewshot] = (),
    avoid_hooks: Sequence[str] = (),
    prompt: Prompt | None = None,
    model: str | None = None,
    moment: str | None = None,
) -> ClipCopy:
    """Hook, title and normalised captions for one candidate, for the platforms this campaign × account allows.

    `avoid_hooks` ("New hook"): hooks this clip already had. They go into the prompt, and a reply that repeats one
    (ignoring case and punctuation) is retried once with a reminder, then refused with RepeatedHookError.

    `moment`: what the moment ranker thinks happens (game footage often says little in words). None uses the
    candidate's own moment reason, so a "New hook" re-render gets it too.
    """
    if moment is None:
        moment = f"{candidate.moment.archetype}: {candidate.moment.reason}" if candidate.moment else ""
    allowed = compliance.allowed_platforms(campaign, platforms)
    if not allowed:
        raise RankError(f"campaign {campaign.name!r} allows none of {[p.value for p in platforms]}")
    prompt = prompt or load_prompt("clip_copy")
    user = prompt.render_user(
        campaign_brief=campaign_brief(campaign),
        fewshots=_render_fewshots(fewshots),
        avoid_hooks=render_avoid_hooks(avoid_hooks),
        platforms=", ".join(p.value for p in allowed),
        start_s=f"{candidate.start_s:.1f}",
        end_s=f"{candidate.end_s:.1f}",
        duration_s=f"{candidate.end_s - candidate.start_s:.1f}",
        clip_text=escape_text(clip_text),
        moment_note=escape_text(moment.strip()) or "(not available)",
    )
    attempts = HOOK_ATTEMPTS if avoid_hooks else 1
    copy: ClipCopy | None = None
    for attempt in range(attempts):
        message = user
        if attempt and copy is not None:
            message += (
                f"\n\nYour last hook, {escape_text(copy.hook)!r}, repeats a hook this clip already had. "
                "Write a clearly different hook (and captions to match)."
            )
        result = client.json_output(
            model=model or client.app.models.copy_,
            system=prompt.system,
            user=message,
            schema=copy_schema(allowed),
            max_tokens=MAX_TOKENS,
            effort=COPY_EFFORT,
        )
        copy = repair_copy(result.data, allowed)
        if not repeats_hook(copy.hook, avoid_hooks):
            return finalize_captions(copy, campaign)
        log.info("copy for candidate %s repeated a used hook (attempt %d)", candidate.id, attempt + 1)
    raise RepeatedHookError(f"the model returned a hook this clip already had ({copy.hook if copy else ''!r})")


@dataclass
class RankResult:
    candidates: list[Candidate] = field(default_factory=list)
    score: ScoreOutcome = field(default_factory=ScoreOutcome)
    windows_built: int = 0
    copy_failures: list[str] = field(default_factory=list)
    prompt_version: str = ""
    failed_batches: list[str] = field(default_factory=list)  # moment ranker; the window ranker uses score
    detail_extra: str = ""  # moment ranker: signal and gate counts
    moment_run: Any = None  # moment ranker: rank.moment_score.MomentRun (every candidate, for reports)

    def detail(self) -> str:
        """The source's RANKED event detail."""
        failed = len(self.failed_batches) + len(self.score.failed_batches)
        text = (
            f"{len(self.candidates)} candidate(s) from {self.windows_built} windows; "
            f"{failed} failed batch(es), {len(self.copy_failures)} copy failure(s)"
        )
        return f"{text}; {self.detail_extra}" if self.detail_extra else text


def rank_source(
    transcript: Transcript,
    campaign: CampaignIn,
    platforms: Sequence[Platform],
    *,
    client: ClaudeClient,
    fewshots: Sequence[Fewshot] = (),
    energy_per_s: Sequence[float] | None = None,
) -> RankResult:
    """Windows → Haiku scores → top N → Sonnet copy, for one source. Candidates whose copy fails are dropped and
    reported. Uses `config.pipeline` for window bounds, how many windows to score and how many candidates to keep."""
    cfg = client.app.pipeline
    score_prompt, copy_prompt = load_prompt("score_windows"), load_prompt("clip_copy")
    version = prompt_version_tag(score_prompt, copy_prompt)
    windows = build_windows(
        transcript,
        min_s=cfg.window_min_s,
        max_s=cfg.window_max_s,
        energy_per_s=energy_per_s,
        keep=cfg.windows_scored_per_source,
    )
    result = RankResult(windows_built=len(windows), prompt_version=version)
    result.score = score_windows(windows, campaign, client=client, fewshots=fewshots, prompt=score_prompt)
    selections: list[Selection] = select_candidates(
        windows,
        result.score.scores,
        words=transcript.words,
        top_n=cfg.top_n_per_source,
        min_clip_s=cfg.min_clip_s,
        max_clip_s=cfg.max_clip_s,
        prompt_version=version,
    )
    for sel in selections:
        try:
            copy = write_copy(
                sel.candidate, sel.text, campaign, platforms, client=client, fewshots=fewshots, prompt=copy_prompt
            )
        except (RankError, compliance.ComplianceError) as exc:
            log.warning("copy failed for %s: %s", sel.window.window_id, exc)
            result.copy_failures.append(f"{sel.window.window_id}: {exc}")
            continue
        result.candidates.append(sel.candidate.model_copy(update={"copy_": copy}))
    return result
