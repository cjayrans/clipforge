"""Interface contract shared by every clipforge module.

Phase 0 owns this file. Phase 1 branches code against it and must not edit it; propose changes in
docs/contract-requests/<branch>.md instead. See docs/ARCHITECTURE.md for how each model is used.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

# --------------------------------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------------------------------


class Marketplace(StrEnum):
    CONTENT_REWARDS = "content_rewards"
    VYRO = "vyro"


class Platform(StrEnum):
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    X = "x"


class CampaignStatus(StrEnum):
    ACTIVE = "active"
    DRAINED = "drained"
    ENDED = "ended"


class SourceStatus(StrEnum):
    REGISTERED = "registered"
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    RANKED = "ranked"
    FAILED = "failed"
    PURGED = "purged"


class ClipState(StrEnum):
    CANDIDATE = "candidate"
    RENDERED = "rendered"
    QA_FAILED = "qa_failed"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    EXPIRED = "expired"


CLIP_TRANSITIONS: dict[ClipState, frozenset[ClipState]] = {
    ClipState.CANDIDATE: frozenset({ClipState.RENDERED, ClipState.EXPIRED}),
    ClipState.RENDERED: frozenset({ClipState.QA_FAILED, ClipState.PENDING_REVIEW}),
    ClipState.QA_FAILED: frozenset(),
    # A re-render (new hook / shorter) sends a clip back to RENDERED and requires a fresh approval.
    ClipState.PENDING_REVIEW: frozenset(
        {ClipState.APPROVED, ClipState.REJECTED, ClipState.RENDERED, ClipState.EXPIRED}
    ),
    ClipState.APPROVED: frozenset({ClipState.SCHEDULED, ClipState.RENDERED, ClipState.EXPIRED}),
    ClipState.REJECTED: frozenset(),
    ClipState.SCHEDULED: frozenset({ClipState.PUBLISHED, ClipState.APPROVED, ClipState.EXPIRED}),
    ClipState.PUBLISHED: frozenset(),
    ClipState.EXPIRED: frozenset(),
}


class PostState(StrEnum):
    QUEUED = "queued"
    PUBLISHING = "publishing"
    LIVE = "live"
    FAILED = "failed"


class SubmissionState(StrEnum):
    AWAITING = "awaiting"
    SUBMITTED = "submitted"
    MISSED = "missed"


class BrandDecision(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class RejectReason(StrEnum):
    OFF_BRIEF = "off_brief"
    BORING = "boring"
    BAD_CROP = "bad_crop"
    BAD_CAPTIONS = "bad_captions"
    OTHER = "other"


class PublishMode(StrEnum):
    DRY_RUN = "dry_run"
    PRIVATE = "private"
    LIVE = "live"


class CropMode(StrEnum):
    FACE = "face"
    CENTER = "center"
    FIT_BLUR = "fit_blur"


# --------------------------------------------------------------------------------------------------
# Campaigns and sources
# --------------------------------------------------------------------------------------------------

Usd = Annotated[float, Field(ge=0)]


class CampaignIn(BaseModel):
    """What the operator registers from a campaign brief (Claude chat produces this JSON)."""

    model_config = ConfigDict(extra="forbid")

    marketplace: Marketplace
    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    submission_url: HttpUrl
    cpm_usd: Usd
    min_payout_usd: Usd = 0.0
    min_views_per_post: int = Field(default=0, ge=0)
    per_clip_cap_usd: Usd | None = None
    budget_remaining_usd: Usd | None = None
    approval_rate_pct: float | None = Field(default=None, ge=0, le=100)
    allowed_platforms: list[Platform] = Field(min_length=1)
    required_tags: list[str] = Field(default_factory=list)
    disclosure_text: str = "#ad"
    rules_notes: str = ""
    is_political: bool = False
    priority: int = Field(default=0, description="Higher publishes first when slots are scarce.")
    source_urls: list[HttpUrl] = Field(default_factory=list)
    # Added for game/publisher campaigns (docs/CAMPAIGNS.md). All optional, so older entries stay valid.
    content_kind: Literal["talk", "game"] = "talk"
    footage_mode: Literal["supplied", "streamer_stream", "own_gameplay", "official_broadcast"] = "supplied"
    allow_stream_download: bool = Field(
        default=False,
        description="True only when the campaign rules explicitly allow downloading the streamer's Twitch VODs/clips.",
    )
    cpm_by_platform: dict[Platform, Usd] = Field(default_factory=dict, description="Overrides cpm_usd per platform.")
    min_payout_by_platform: dict[Platform, Usd] = Field(default_factory=dict)
    unpaid_viewer_countries: list[str] = Field(
        default_factory=list, description="Views from these countries earn nothing; record them from the brief."
    )
    participants: int | None = Field(default=None, ge=0)
    budget_total_usd: Usd | None = None

    def cpm_for(self, platform: Platform) -> float:
        """Rate per 1,000 views on one platform (falls back to cpm_usd)."""
        return self.cpm_by_platform.get(platform, self.cpm_usd)

    def min_payout_for(self, platform: Platform) -> float:
        return self.min_payout_by_platform.get(platform, self.min_payout_usd)


class Campaign(CampaignIn):
    model_config = ConfigDict(extra="forbid")

    id: int
    status: CampaignStatus
    created_at: datetime


# --------------------------------------------------------------------------------------------------
# Transcription
# --------------------------------------------------------------------------------------------------


class Word(BaseModel):
    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    prob: float | None = Field(default=None, ge=0, le=1)


class Segment(BaseModel):
    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)


class Transcript(BaseModel):
    source_id: int
    provider: Literal["groq", "faster_whisper"]
    model: str
    language: str | None = None
    words: list[Word]
    segments: list[Segment]
    duration_s: float = Field(ge=0)


# --------------------------------------------------------------------------------------------------
# Ranking
# --------------------------------------------------------------------------------------------------


class Window(BaseModel):
    window_id: str
    source_id: int
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    text: str
    prescore: float = 0.0

    @model_validator(mode="after")
    def _length(self) -> Window:
        length = self.end_s - self.start_s
        if not 20.0 <= length <= 75.0:
            raise ValueError(f"window length {length:.1f}s outside 20-75s")
        return self


class WindowScore(BaseModel):
    """One item of the Haiku `score_windows` tool output."""

    window_id: str
    score: int = Field(ge=0, le=100)
    standalone: bool = Field(description="Makes sense with no prior context.")
    brief_ok: bool = Field(description="Complies with the campaign brief and rules.")
    hook_potential: int = Field(ge=0, le=10)
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    reasons: str = Field(max_length=400)


class PlatformCaption(BaseModel):
    platform: Platform
    text: str


class ClipCopy(BaseModel):
    """Sonnet output for one candidate; captions are normalised by compliance.normalize_caption."""

    hook: str = Field(max_length=60, description="On-screen hook, at most 8 words.")
    youtube_title: str = Field(max_length=60)
    captions: list[PlatformCaption]

    @model_validator(mode="after")
    def _hook_words(self) -> ClipCopy:
        if len(self.hook.split()) > 8:
            raise ValueError("hook must be 8 words or fewer")
        return self


class Candidate(BaseModel):
    id: int | None = None
    source_id: int
    start_s: float
    end_s: float
    score: int
    standalone: bool
    brief_ok: bool
    reasons: str
    copy_: ClipCopy | None = Field(default=None, alias="copy")
    prompt_version: str
    state: ClipState = ClipState.CANDIDATE


# --------------------------------------------------------------------------------------------------
# Rendering and QA
# --------------------------------------------------------------------------------------------------


class ShotCrop(BaseModel):
    start_s: float
    end_s: float
    mode: CropMode
    x: int = Field(ge=0, description="Left edge of the crop in source pixels (ignored for fit_blur).")
    w: int = Field(gt=0)
    h: int = Field(gt=0)


class CropPlan(BaseModel):
    src_w: int
    src_h: int
    shots: list[ShotCrop]


class RenderManifest(BaseModel):
    clip_path: str
    candidate_id: int
    account_set: str
    style_preset: str
    width: int
    height: int
    fps: float
    duration_s: float
    has_ad_label: bool
    ad_label_start_s: float
    ad_label_end_s: float
    hook_text: str
    hook_end_s: float
    audio_source_only: bool = Field(description="True when only the source audio stream is mapped.")
    sha256: str


class QACheck(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class QAReport(BaseModel):
    clip_path: str
    checks: list[QACheck]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


# --------------------------------------------------------------------------------------------------
# Review, publishing, tracking
# --------------------------------------------------------------------------------------------------


class Approval(BaseModel):
    clip_id: int
    decision: ApprovalDecision
    reason: RejectReason | None = None
    decided_at: datetime
    actor: Literal["operator"] = "operator"


class Slot(BaseModel):
    at: datetime  # timezone-aware, America/New_York
    clip_id: int | None = None


class PublishRequest(BaseModel):
    post_id: int
    clip_id: int
    platform: Platform
    upload_post_user: str
    video_path: str
    caption: str
    youtube_title: str | None = None
    mode: PublishMode


class PublishResult(BaseModel):
    post_id: int
    platform: Platform
    accepted: bool
    post_url: str | None = None
    published_at: datetime | None = None
    upload_post_ref: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class SubmissionNotice(BaseModel):
    """What the bot sends when a post goes live."""

    submission_id: int
    campaign_name: str
    submission_url: str
    post_url: str
    platform: Platform
    deadline_at: datetime


class ReconcileRow(BaseModel):
    """CSV row for make export-reconcile / import-reconcile."""

    submission_id: int
    campaign_name: str
    platform: Platform
    post_url: str
    submitted_at: datetime | None
    brand_decision: BrandDecision
    views: int | None = Field(default=None, ge=0)
    paid_usd: Usd | None = None
    note: str = ""
