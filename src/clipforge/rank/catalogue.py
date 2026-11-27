"""Prompt text for the moment catalogue, reject list and rubric of a moment profile (shared by every moment prompt).

The catalogue is rendered as examples of what good looks like. The prompts say in so many words that it is not a
list of allowed moments: any moment that scores well on the rubric is valid (archetype `other`).
"""

from __future__ import annotations

from collections.abc import Sequence

from clipforge.rank.profile import MomentProfile, Rule
from clipforge.rank.prompts import escape_text

CATALOGUE_NOTE = (
    "These are examples of what good looks like, not a list of allowed moments. Propose any moment that scores well "
    "on the rubric; use archetype `other` when none of these fits."
)


def render_rules(rules: Sequence[Rule]) -> str:
    return "\n".join(f"- `{r.id}` ({r.label}): {escape_text(r.description)}" for r in rules) or "(none)"


def render_catalogue(profile: MomentProfile) -> str:
    blocks = []
    for a in profile.archetypes:
        lines = [f"- `{a.id}` ({a.label}): {escape_text(a.description)}"]
        if a.examples:
            lines.append("  Examples: " + "; ".join(escape_text(x) for x in a.examples) + ".")
        if a.signs:
            lines.append("  Signs: " + "; ".join(escape_text(x) for x in a.signs) + ".")
        blocks.append("\n".join(lines))
    return CATALOGUE_NOTE + "\n" + "\n".join(blocks)


def render_rejects(profile: MomentProfile) -> str:
    return "\n".join(f"- {escape_text(x)}" for x in profile.reject_examples) or "(none)"


def render_rubric(profile: MomentProfile) -> str:
    return "\n".join(
        f"- `{c.id}` ({c.label}, weight {profile.weights[c.id]:g}): {escape_text(c.description)}"
        for c in profile.rubric
    )


def catalogue_values(profile: MomentProfile) -> dict[str, object]:
    """Prompt variables every moment prompt shares."""
    w = profile.window
    return {
        "ranker_notes": escape_text(profile.ranker_notes.strip()),
        "catalogue": render_catalogue(profile),
        "rejects": render_rejects(profile),
        "rubric": render_rubric(profile),
        "skip_rules": render_rules(profile.skip_rules),
        "review_flags": render_rules(profile.review_flags),
        "min_s": f"{w.min_s:g}",
        "target_min_s": f"{w.target_min_s:g}",
        "target_max_s": f"{w.target_max_s:g}",
        "max_s": f"{w.max_s:g}",
        "post_roll_s": f"{w.post_roll_s:g}",
    }
