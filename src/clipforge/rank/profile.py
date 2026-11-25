"""Moment profiles: everything niche-specific about moment selection, as data (config/moment_profiles/<name>.yaml).

A campaign opts in with `"moment_profile": "<name>"` in its JSON. The code in rank/moments.py, rank/signals.py and
rank/moment_score.py is generic; archetypes, weights, gate thresholds, window rules, trigger/reaction word lists,
skip rules (e.g. a publisher's cutscene policy) and content flags all come from the profile. Changing weights means
bumping `weights_version`, which is stored on every candidate so calibration can compare versions.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml  # type: ignore[import-untyped]  # no types-PyYAML in the lockfile
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from clipforge import compliance
from clipforge.render.assets import project_root

PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ProfileError(ValueError):
    """A moment profile is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Rule(_Strict):
    """A labelled category the ranker can assign (archetype, skip rule or review flag)."""

    id: str
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"id {v!r} must be lowercase letters, digits and underscores (max 32)")
        return v


class SkipRule(Rule):
    action: Literal["reject", "penalize"]
    penalty: float = Field(default=0.5, gt=0, le=1, description="Score multiplier when action is penalize.")


class Weights(_Strict):
    reaction: float = Field(ge=0)
    event: float = Field(ge=0)
    setup: float = Field(ge=0)
    absurdity: float = Field(ge=0)

    @model_validator(mode="after")
    def _sum(self) -> Weights:
        total = self.reaction + self.event + self.setup + self.absurdity
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total:.3f}")
        return self


class WindowRules(_Strict):
    """Clip window: start pre_roll before the trigger, end post_roll after the payoff, length within bounds."""

    pre_roll_min_s: float = Field(ge=0)
    pre_roll_s: float = Field(ge=0)
    pre_roll_max_s: float = Field(ge=0)
    post_roll_s: float = Field(ge=0, le=10)
    min_s: float
    target_max_s: float
    max_s: float
    chain_gap_s: float = Field(gt=0, le=15, description="Reactions closer than this extend the same payoff.")

    @model_validator(mode="after")
    def _bounds(self) -> WindowRules:
        if not self.pre_roll_min_s <= self.pre_roll_s <= self.pre_roll_max_s:
            raise ValueError("need pre_roll_min_s <= pre_roll_s <= pre_roll_max_s")
        if not compliance.MIN_CLIP_S <= self.min_s <= self.target_max_s <= self.max_s <= compliance.MAX_CLIP_S:
            raise ValueError(
                f"need {compliance.MIN_CLIP_S} <= min_s <= target_max_s <= max_s <= {compliance.MAX_CLIP_S}"
            )
        return self


class SignalRules(_Strict):
    baseline_s: float = Field(gt=5, description="Rolling baseline for loudness and motion z-scores.")
    loud_z: float = Field(gt=0, description="Loudness z-score that counts as a spike.")
    motion_z: float = Field(gt=0, description="Frame-difference z-score that counts as a motion spike.")
    motion_min_diff: float = Field(ge=0, description="Ignore motion spikes below this mean pixel difference.")
    silence_beat_s: float = Field(gt=0, description="A speech gap at least this long, then a reaction.")
    silence_reaction_s: float = Field(gt=0, description="The reaction must start within this after the gap.")
    motion_keyframes_only: bool = Field(default=True, description="Decode keyframes only (cheap) for motion.")
    motion_sample_fps: float = Field(default=2.0, gt=0, le=10, description="Used when motion_keyframes_only=false.")


class Lexicon(_Strict):
    """Lowercase words/phrases matched against the transcript (punctuation ignored)."""

    exclamations: list[str] = Field(default_factory=list, description="Single words; count when stressed.")
    exclamation_phrases: list[str] = Field(default_factory=list, description="Multi-word; always count.")
    laughter_patterns: list[str] = Field(default_factory=list, description="Regexes for one transcript word.")
    cues: list[str] = Field(default_factory=list, description="Words/phrases that announce an event (trigger).")
    chat: list[str] = Field(default_factory=list, description="Words that address chat (chat interplay trigger).")
    sensitive: list[str] = Field(default_factory=list, description="Words/phrases that flag a clip for review.")

    @field_validator("exclamations", "exclamation_phrases", "cues", "chat", "sensitive")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [" ".join(x.lower().split()) for x in v if x.strip()]

    @field_validator("laughter_patterns")
    @classmethod
    def _regex(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"laughter pattern {pattern!r}: {exc}") from exc
        return v


class GateRules(_Strict):
    reaction_within_s: float = Field(gt=0, le=10, description="A reaction must follow the trigger within this.")
    reaction_lead_s: float = Field(ge=0, le=2, description="A reaction may start this much before the trigger.")
    min_audio_std_db: float = Field(ge=0, description="Below this loudness spread a window needs a visual event.")
    max_candidates: int = Field(gt=0, le=200, description="Candidates sent to the ranker per source.")
    min_score: float = Field(ge=0, le=100, description="Weighted score (0-100) a candidate needs to be kept.")


class MomentProfile(_Strict):
    name: str
    weights_version: str = Field(min_length=1, max_length=64)
    description: str = ""
    ranker_notes: str = Field(default="", max_length=2000, description="Niche guidance added to the ranker prompt.")
    archetypes: list[Rule] = Field(min_length=1)
    weights: Weights
    window: WindowRules
    signals: SignalRules
    lexicon: Lexicon
    gates: GateRules
    skip_rules: list[SkipRule] = Field(default_factory=list)
    review_flags: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> MomentProfile:
        for kind, rules in (
            ("archetypes", self.archetypes),
            ("skip_rules", self.skip_rules),
            ("review_flags", self.review_flags),
        ):
            ids = [r.id for r in rules]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{kind} ids must be unique")
        if "other" not in {a.id for a in self.archetypes}:
            raise ValueError("archetypes must include 'other'")
        return self

    @property
    def archetype_ids(self) -> list[str]:
        return [a.id for a in self.archetypes]

    def skip_rule(self, rule_id: str) -> SkipRule | None:
        return next((r for r in self.skip_rules if r.id == rule_id), None)


def profiles_dir() -> Path:
    return project_root() / "config" / "moment_profiles"


def parse_profile(text: str, *, expected_name: str | None = None) -> MomentProfile:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileError(f"moment profile is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProfileError("moment profile must be a mapping")
    try:
        profile = MomentProfile.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(x) for x in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]
        raise ProfileError("moment profile is invalid:\n" + "\n".join(lines)) from exc
    if expected_name is not None and profile.name != expected_name:
        raise ProfileError(f"moment profile name {profile.name!r} does not match file {expected_name!r}")
    return profile


def load_profile(name: str, directory: Path | None = None) -> MomentProfile:
    """Load `<directory>/<name>.yaml` (default config/moment_profiles/). Raises ProfileError."""
    if not PROFILE_NAME_RE.match(name):
        raise ProfileError(f"moment profile name {name!r} is not a plain file name")
    path = (directory or profiles_dir()) / f"{name}.yaml"
    if not path.is_file():
        raise ProfileError(f"moment profile not found: {path}")
    return parse_profile(path.read_text(encoding="utf-8"), expected_name=name)
