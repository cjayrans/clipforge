"""Moment profiles: everything niche-specific about moment selection, as data (config/moment_profiles/<name>.yaml).

A campaign opts in with `"moment_profile": "<name>"` in its JSON. The code in rank/ is generic; the moment catalogue
(archetypes with examples and signs: examples of what good looks like, never a list of allowed moments), the reject
examples, the scoring rubric and its weights, gate thresholds, window rules, the content pass and vision settings,
trigger/reaction word lists, skip rules (e.g. a publisher's cutscene policy) and content flags all come from the
profile. Changing the rubric, weights or catalogue means bumping `weights_version`, which is stored on every candidate
so calibration can compare versions.
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


class Archetype(Rule):
    """One catalogue entry: an example moment type with concrete examples and the signs that it is happening."""

    examples: list[str] = Field(default_factory=list, max_length=12)
    signs: list[str] = Field(default_factory=list, max_length=12)
    copy_hint: str = Field(default="", max_length=200, description="Extra guidance for the copywriter.")


class Criterion(Rule):
    """One rubric criterion, scored 0-10 by the ranker."""

    description: str = Field(min_length=1, max_length=600)


class WindowRules(_Strict):
    """Clip window: open on the most interesting beat a stranger can follow (no dead air), end right after the payoff.

    `min_s`/`max_s` are hard bounds; `target_min_s`..`target_max_s` is what the ranker aims for. A moment that runs
    past its proposed window is extended up to `max_s`, never truncated (rank/moment_score.apply_window_rules)."""

    min_s: float
    target_min_s: float
    target_max_s: float
    max_s: float
    post_roll_s: float = Field(ge=0, le=10, description="At most this much after the payoff.")
    max_lead_silence_s: float = Field(ge=0, le=5, description="At most this much silence before the first beat.")
    context_s: float = Field(ge=0, le=120, description="Transcript shown either side of a candidate.")
    chain_gap_s: float = Field(gt=0, le=15, description="Reactions closer than this extend the same payoff.")

    @model_validator(mode="after")
    def _bounds(self) -> WindowRules:
        lo, hi = compliance.MIN_CLIP_S, compliance.MAX_CLIP_S
        if not lo <= self.min_s <= self.target_min_s <= self.target_max_s <= self.max_s <= hi:
            raise ValueError(f"need {lo} <= min_s <= target_min_s <= target_max_s <= max_s <= {hi}")
        return self


class ContentPassRules(_Strict):
    """Content-first moment finding: the whole transcript in overlapping segments (rank/content_pass.py)."""

    segment_s: float = Field(ge=120, le=3600)
    overlap_s: float = Field(ge=30, le=900)
    max_proposals_per_segment: int = Field(gt=0, le=40)
    edge_s: float = Field(default=5.0, ge=0, le=60, description="A proposal this close to a segment edge is 'edge'.")

    @model_validator(mode="after")
    def _overlap(self) -> ContentPassRules:
        if self.overlap_s * 2 > self.segment_s:
            raise ValueError("content_pass overlap_s must be at most half of segment_s")
        return self


class VisionRules(_Strict):
    """Keyframe check on the shortlist (rank/vision.py)."""

    enabled: bool = True
    shortlist: int = Field(default=12, ge=0, le=40, description="Best candidates by transcript score that get it.")
    max_frames: int = Field(default=8, ge=1, le=20)
    frame_every_s: float = Field(default=2.5, gt=0, le=10)
    frame_width: int = Field(default=768, ge=256, le=1568)
    facecam_crop: bool = True
    jpeg_quality: int = Field(default=80, ge=30, le=95)
    # What the frames can add (profile rule ids, all optional): a skip rule when the frames show a menu, loading
    # screen or just chatting with no event; when most frames are a cutscene with no visible reaction (reject) or
    # with one (penalize); when they show an ending or story reveal; a review flag for graphic or sexual frames.
    no_event_skip: str | None = None
    cutscene_skip: str | None = None
    cutscene_part_skip: str | None = None
    spoiler_skip: str | None = None
    graphic_review: str | None = None


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
    reaction_within_s: float = Field(gt=0, le=10, description="Signal pairing: a reaction this soon after a trigger.")
    reaction_lead_s: float = Field(ge=0, le=2, description="A reaction may start this much before the trigger.")
    min_audio_std_db: float = Field(ge=0, description="Loudness spread recorded as evidence (never a gate).")
    max_candidates: int = Field(gt=0, le=200, description="Merged candidates sent to the ranker per source.")
    min_score: float = Field(ge=0, le=100, description="Weighted score (0-100) a candidate needs to be kept.")


class MomentProfile(_Strict):
    name: str
    weights_version: str = Field(min_length=1, max_length=64)
    description: str = ""
    ranker_notes: str = Field(default="", max_length=2000, description="Niche guidance added to the ranker prompt.")
    archetypes: list[Archetype] = Field(min_length=1)
    reject_examples: list[str] = Field(default_factory=list, max_length=30)
    rubric: list[Criterion] = Field(min_length=1, max_length=10)
    weights: dict[str, float]
    window: WindowRules
    content_pass: ContentPassRules
    vision: VisionRules = Field(default_factory=VisionRules)
    signals: SignalRules
    lexicon: Lexicon
    gates: GateRules
    skip_rules: list[SkipRule] = Field(default_factory=list)
    review_flags: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> MomentProfile:
        for kind, rules in (
            ("archetypes", self.archetypes),
            ("rubric", self.rubric),
            ("skip_rules", self.skip_rules),
            ("review_flags", self.review_flags),
        ):
            ids = [r.id for r in rules]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{kind} ids must be unique")
        if "other" not in {a.id for a in self.archetypes}:
            raise ValueError("archetypes must include 'other'")
        skip_ids = {r.id for r in self.skip_rules}
        v = self.vision
        for name in ("no_event_skip", "cutscene_skip", "cutscene_part_skip", "spoiler_skip"):
            value = getattr(v, name)
            if value is not None and value not in skip_ids:
                raise ValueError(f"vision.{name} {value!r} is not a skip rule id")
        if v.graphic_review is not None and v.graphic_review not in {r.id for r in self.review_flags}:
            raise ValueError(f"vision.graphic_review {v.graphic_review!r} is not a review flag id")
        if set(self.weights) != set(self.rubric_ids):
            raise ValueError(f"weights must have exactly the rubric ids {self.rubric_ids}")
        if any(w < 0 for w in self.weights.values()):
            raise ValueError("weights must be >= 0")
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"weights must sum to 1.0, got {total:.3f}")
        return self

    @property
    def rubric_ids(self) -> list[str]:
        return [c.id for c in self.rubric]

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
