"""Interface contract shared by every clipforge module.

Phase 0 owns this file. Phase 1 branches code against it and must not edit it; propose changes in
docs/contract-requests/<branch>.md instead. See docs/ARCHITECTURE.md for how each model is used.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

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


# Phase 0 addition (ARCHITECTURE.md §2). A retry re-sends the same post row (request_id = post id); no new state.
POST_TRANSITIONS: dict[PostState, frozenset[PostState]] = {
    PostState.QUEUED: frozenset({PostState.PUBLISHING, PostState.FAILED}),
    PostState.PUBLISHING: frozenset({PostState.LIVE, PostState.FAILED}),
    PostState.LIVE: frozenset(),
    # Operator repair only (`cf post repair`, publish/repair.py): a post marked failed that Upload-Post reports as
    # completed goes back to PUBLISHING so the poller can finish it. Never re-sent: it keeps its upload_post_job.
    PostState.FAILED: frozenset({PostState.PUBLISHING}),
}


class SubmissionState(StrEnum):
    AWAITING = "awaiting"
    SUBMITTED = "submitted"
    MISSED = "missed"


# Phase 0 addition (ARCHITECTURE.md §2).
SUBMISSION_TRANSITIONS: dict[SubmissionState, frozenset[SubmissionState]] = {
    SubmissionState.AWAITING: frozenset({SubmissionState.SUBMITTED, SubmissionState.MISSED}),
    SubmissionState.SUBMITTED: frozenset(),
    SubmissionState.MISSED: frozenset(),
}


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


class SourceHost(StrEnum):
    """Phase 0 addition: how a source URL is classified (compliance.classify_source_host).

    Downloadable: GDRIVE, DROPBOX, DIRECT, INBOX, and TWITCH only with campaign permission.
    Never downloaded: YOUTUBE, KICK, OTHER.
    """

    GDRIVE = "gdrive"
    DROPBOX = "dropbox"
    TWITCH = "twitch"
    DIRECT = "direct"
    INBOX = "inbox"
    YOUTUBE = "youtube"
    KICK = "kick"
    OTHER = "other"


# --------------------------------------------------------------------------------------------------
# Campaigns and sources
# --------------------------------------------------------------------------------------------------

Usd = Annotated[float, Field(ge=0)]


class OverlayPosition(BaseModel):
    """Centre of a campaign overlay image, in percent of the 1080x1920 frame."""

    model_config = ConfigDict(extra="forbid")

    x_pct: float = Field(default=50.0, ge=0, le=100)
    y_pct: float = Field(ge=0, le=100)


class CampaignOverlay(BaseModel):
    """A campaign-supplied image (PNG with alpha) composited over every clip of that campaign.

    Render places it, keeps the "Ad" label clear of it and refuses any placement that would cover the captions,
    the hook or the platform UI zones (render.campaign_overlay). The file lives under `campaigns/assets/` and is
    not committed.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Repo-relative path, under campaigns/assets/, of a .png file.")
    position: OverlayPosition
    scale: float = Field(gt=0, le=1, description="Image width as a fraction of the 1080 px frame width.")
    full_duration: bool = Field(
        default=True, description="True: shown for the whole clip. False: shown from the end of the hook."
    )

    @field_validator("path")
    @classmethod
    def _path_under_assets(cls, v: str) -> str:
        parts = v.replace("\\", "/").split("/")
        if v.startswith("/") or ".." in parts or parts[:2] != ["campaigns", "assets"] or len(parts) < 3:
            raise ValueError("overlay.path must be a relative path under campaigns/assets/")
        if not v.lower().endswith(".png"):
            raise ValueError("overlay.path must be a .png file")
        return v


class CampaignIn(BaseModel):
    """What the operator registers from a campaign brief (Claude chat produces this JSON)."""

    model_config = ConfigDict(extra="forbid")

    marketplace: Marketplace
    name: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    submission_url: HttpUrl
    cpm_usd: Usd
    min_payout_usd: Usd = Field(default=0.0, description="null in the JSON (not known yet) is stored as 0.")
    min_views_per_post: int = Field(default=0, ge=0, description="null in the JSON is stored as 0.")
    per_clip_cap_usd: Usd | None = None
    budget_remaining_usd: Usd | None = None
    approval_rate_pct: float | None = Field(default=None, ge=0, le=100)
    allowed_platforms: list[Platform] = Field(min_length=1)
    required_tags: list[str] = Field(
        default_factory=list,
        description="Text every caption must end with (tags, or a link like a channel URL), in this order. "
        "The JSON may also call it `required_caption_tags`.",
    )
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
    # Added for campaigns with an overlay, a caption tone or stream-only footage. All optional and generic: a new
    # campaign is one JSON file (plus its assets), never a code change.
    overlay: CampaignOverlay | None = None
    copy_style_notes: str = Field(
        default="", max_length=1000, description="Tone for hooks and captions from the brief, fed to the copy prompt."
    )
    stream_channels: list[str] = Field(
        default_factory=list,
        description="Twitch channel logins whose VODs may be downloaded; empty = any registered Twitch URL.",
    )
    max_source_minutes: int | None = Field(
        default=None, ge=1, description="Download only the first N minutes of a stream source (Twitch)."
    )
    # Added for moment selection (docs/research/clipping-gta6-moment-types.md): the name of a file in
    # config/moment_profiles/ (archetypes, weights, gates, window rules). None keeps the transcript-window ranker.
    moment_profile: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")

    @model_validator(mode="before")
    @classmethod
    def _caption_tags_alias(cls, data: object) -> object:
        """`required_caption_tags` is another name for `required_tags` (one of the two, not both)."""
        if isinstance(data, dict) and "required_caption_tags" in data:
            if "required_tags" in data:
                raise ValueError("give required_tags or required_caption_tags, not both")
            data = {**data, "required_tags": data["required_caption_tags"]}
            del data["required_caption_tags"]
        return data

    @field_validator("min_payout_usd", "min_views_per_post", mode="before")
    @classmethod
    def _unknown_is_zero(cls, v: object) -> object:
        return 0 if v is None else v

    @field_validator("stream_channels")
    @classmethod
    def _channel_logins(cls, v: list[str]) -> list[str]:
        out = [c.strip().lower() for c in v if c.strip()]
        for c in out:
            if not re.fullmatch(r"[a-z0-9_]{1,25}", c):
                raise ValueError(f"stream_channels: {c!r} is not a Twitch login (letters, digits, underscore)")
        return out

    def cpm_for(self, platform: Platform) -> float:
        """Rate per 1,000 views on one platform (falls back to cpm_usd)."""
        return self.cpm_by_platform.get(platform, self.cpm_usd)

    def min_payout_for(self, platform: Platform) -> float:
        return self.min_payout_by_platform.get(platform, self.min_payout_usd)

    @property
    def slug(self) -> str:
        """Phase 0 addition: filesystem-safe name, used for `inbox/<slug>/`."""
        return slugify(self.name)


def slugify(text: str) -> str:
    """Phase 0 addition: lowercase ASCII words joined by '-'.

    'AsianJeff Clipping (Reach)' -> 'asianjeff-clipping-reach'.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "campaign"


class Campaign(CampaignIn):
    model_config = ConfigDict(extra="forbid")

    id: int
    status: CampaignStatus
    created_at: datetime


class Source(BaseModel):
    """Phase 0 addition: one registered footage file of a campaign (mirrors the `sources` table)."""

    id: int
    campaign_id: int
    url: str = Field(description="Normalized URL as registered (http(s) or file://inbox/...).")
    status: SourceStatus = SourceStatus.REGISTERED
    sha256: str | None = None
    local_path: str | None = None
    duration_s: float | None = Field(default=None, ge=0)
    fail_reason: str | None = Field(default=None, description="e.g. 'no_speech' (PRD F17), 'host_refused'.")
    folder_url: str | None = Field(default=None, description="Folder link this file was listed from (PRD F17).")
    downloaded_at: datetime | None = None
    purged_at: datetime | None = None


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


class GateResult(BaseModel):
    """Added for moment selection: one gate a moment candidate went through. `passed` None = not evaluated (the
    signal it needs was unavailable)."""

    name: str
    passed: bool | None
    detail: str = ""


class MomentScore(BaseModel):
    """Added for moment selection: how the moment ranker judged a candidate (stored per candidate for calibration).

    The four scores are 0-10; `weighted` is the profile-weighted sum on a 0-100 scale after skip-rule penalties.
    `review_flags` are content flags (e.g. sexual, graphic) shown on the Telegram preview: never an auto-reject.
    """

    profile: str
    weights_version: str
    archetype: str
    reaction: int = Field(ge=0, le=10)
    event: int = Field(ge=0, le=10)
    setup: int = Field(ge=0, le=10)
    absurdity: int = Field(ge=0, le=10)
    weighted: float = Field(ge=0, le=100)
    reason: str = Field(default="", max_length=300)
    skip_flags: list[str] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    gates: list[GateResult] = Field(default_factory=list)


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
    moment: MomentScore | None = None  # set by the moment ranker only


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


class Rect(BaseModel):
    """Pixel rectangle in the 1080x1920 frame: x0/y0 inclusive, x1/y1 exclusive."""

    x0: int
    y0: int
    x1: int
    y1: int

    def intersects(self, other: Rect) -> bool:
        return self.x0 < other.x1 and other.x0 < self.x1 and self.y0 < other.y1 and other.y0 < self.y1


class OverlayManifest(BaseModel):
    """The campaign overlay as rendered: placed image rect, its visible (non-transparent) part, and timing."""

    image_sha256: str
    rect: Rect
    opaque_rect: Rect
    start_s: float
    end_s: float
    full_duration: bool


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
    # Where the "Ad" label was drawn, and the campaign overlay if the campaign has one (invariant 3: never overlap).
    ad_label_rect: Rect | None = None
    overlay: OverlayManifest | None = None
    # Label layout version (compliance.AD_LABEL_LAYOUT). 1 = before 2026-11-25 (top-left corner, under the platform
    # UI); 2 = below the hook, out of the platform UI zones, on a semi-opaque dark box. Older manifests default to 1.
    ad_label_layout: int = Field(default=1, ge=1)
    ad_label_box: bool = Field(default=False, description="The label style draws a semi-opaque dark box behind it.")


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


class Evening(BaseModel):
    """Phase 0 addition: one publishing evening (mirrors the `evenings` table). Nothing publishes unless armed."""

    date: date  # America/New_York calendar date
    armed_at: datetime | None = None
    skipped: bool = False
    slots: list[Slot] = Field(default_factory=list)

    @property
    def armed(self) -> bool:
        return self.armed_at is not None and not self.skipped


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
