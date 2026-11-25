"""SQLAlchemy 2.0 models for every table in docs/ARCHITECTURE.md §3 (plus the columns later phases need).

Conventions:
- Row classes end in `Row` so they never clash with the pydantic models in `clipforge.schemas`.
- Datetimes are stored as UTC and always come back timezone-aware (`UTCDateTime`); naive datetimes are refused.
- Enum columns store the `StrEnum` value with a CHECK constraint.
- `state` columns can only change through `clipforge.db.transitions.transition()`, which enforces the transition
  tables in `schemas` and writes an `events` row.

Phase 0 owns this package; Phase 1 branches must not edit it (see docs/TASKS.md).
"""

from __future__ import annotations

import contextvars
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Dialect,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from clipforge.schemas import (
    CLIP_TRANSITIONS,
    POST_TRANSITIONS,
    SUBMISSION_TRANSITIONS,
    ApprovalDecision,
    BrandDecision,
    CampaignStatus,
    ClipState,
    Marketplace,
    Platform,
    PostState,
    RejectReason,
    SourceStatus,
    SubmissionState,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as naive UTC; returns aware UTC. Refuses naive input."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime: store timezone-aware datetimes (UTC)")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def _enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=32,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: UTCDateTime(), dict[str, Any]: JSON, list[Any]: JSON}


# --------------------------------------------------------------------------------------------------
# State guard: `state` may only change inside transitions.transition()
# --------------------------------------------------------------------------------------------------

IN_TRANSITION: contextvars.ContextVar[bool] = contextvars.ContextVar("clipforge_in_transition", default=False)


class IllegalTransition(ValueError):
    """A state change not allowed by the schemas transition tables (or made outside transition())."""


def _guard_state(
    row: Any, old: StrEnum | None, new: StrEnum, table: dict[Any, frozenset[Any]], initial: frozenset[Any]
) -> None:
    if old is None:
        if new not in initial:
            raise IllegalTransition(f"{type(row).__name__} must start in {sorted(initial)}, not {new}")
        return
    if old == new:
        return
    if not IN_TRANSITION.get():
        raise IllegalTransition(f"{type(row).__name__}.state must change via db.transitions.transition()")
    if new not in table[old]:
        raise IllegalTransition(f"{type(row).__name__}: {old} -> {new} is not allowed")


# --------------------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------------------


class CampaignRow(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), unique=True)
    platform_marketplace: Mapped[Marketplace] = mapped_column(_enum(Marketplace, "marketplace"))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    submission_url: Mapped[str] = mapped_column(Text)
    cpm_usd: Mapped[float] = mapped_column(Float)
    min_payout_usd: Mapped[float] = mapped_column(Float, default=0.0)
    min_views_per_post: Mapped[int] = mapped_column(Integer, default=0)
    per_clip_cap_usd: Mapped[float | None] = mapped_column(Float)
    budget_remaining_usd: Mapped[float | None] = mapped_column(Float)
    budget_total_usd: Mapped[float | None] = mapped_column(Float)
    approval_rate_pct: Mapped[float | None] = mapped_column(Float)
    allowed_platforms: Mapped[list[Any]] = mapped_column(JSON)
    required_tags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    disclosure_text: Mapped[str] = mapped_column(Text, default="#ad")
    rules_notes: Mapped[str] = mapped_column(Text, default="")
    is_political: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    source_urls: Mapped[list[Any]] = mapped_column(JSON, default=list)
    content_kind: Mapped[str] = mapped_column(String(16), default="talk")
    footage_mode: Mapped[str] = mapped_column(String(32), default="supplied")
    allow_stream_download: Mapped[bool] = mapped_column(Boolean, default=False)
    cpm_by_platform: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    min_payout_by_platform: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    unpaid_viewer_countries: Mapped[list[Any]] = mapped_column(JSON, default=list)
    participants: Mapped[int | None] = mapped_column(Integer)
    overlay: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    copy_style_notes: Mapped[str] = mapped_column(Text, default="")
    stream_channels: Mapped[list[Any]] = mapped_column(JSON, default=list)
    max_source_minutes: Mapped[int | None] = mapped_column(Integer)
    moment_profile: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[CampaignStatus] = mapped_column(
        _enum(CampaignStatus, "campaign_status"), default=CampaignStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class SourceRow(Base):
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("campaign_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    folder_url: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    local_path: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    status: Mapped[SourceStatus] = mapped_column(_enum(SourceStatus, "source_status"), default=SourceStatus.REGISTERED)
    fail_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column()
    purged_at: Mapped[datetime | None] = mapped_column()


class TranscriptRow(Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    words_json_path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(16))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CandidateRow(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    score: Mapped[int] = mapped_column(Integer)
    standalone: Mapped[bool] = mapped_column(Boolean)
    brief_ok: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str | None] = mapped_column(String(120))
    title: Mapped[str | None] = mapped_column(String(120))
    captions_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Hooks this candidate had before its current one ("New hook" never repeats any of them).
    hook_history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str] = mapped_column(String(64))
    state: Mapped[ClipState] = mapped_column(_enum(ClipState, "candidate_state"), default=ClipState.CANDIDATE)
    # Moment ranker calibration (migration 0003). All null for transcript-window candidates and older rows.
    moment_profile: Mapped[str | None] = mapped_column(String(64))
    weights_version: Mapped[str | None] = mapped_column(String(64))
    archetype: Mapped[str | None] = mapped_column(String(32), index=True)
    score_reaction: Mapped[int | None] = mapped_column(Integer)
    score_event: Mapped[int | None] = mapped_column(Integer)
    score_setup: Mapped[int | None] = mapped_column(Integer)
    score_absurdity: Mapped[int | None] = mapped_column(Integer)
    gate_results_json: Mapped[list[Any] | None] = mapped_column(JSON)
    skip_flags: Mapped[list[Any] | None] = mapped_column(JSON)
    review_flags: Mapped[list[Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @validates("state")
    def _check_state(self, _key: str, value: ClipState | str) -> ClipState:
        new = ClipState(value)
        _guard_state(self, self.state, new, CLIP_TRANSITIONS, frozenset({ClipState.CANDIDATE}))
        return new


class ClipRow(Base):
    __tablename__ = "clips"
    __table_args__ = (UniqueConstraint("candidate_id", "account_set"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    account_set: Mapped[str] = mapped_column(String(64))
    style_preset: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(Text)
    duration_s: Mapped[float] = mapped_column(Float)
    sha256: Mapped[str] = mapped_column(String(64))
    qa_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    state: Mapped[ClipState] = mapped_column(_enum(ClipState, "clip_state"), default=ClipState.RENDERED)
    rendered_at: Mapped[datetime] = mapped_column(default=utcnow)
    review_message_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @validates("state")
    def _check_state(self, _key: str, value: ClipState | str) -> ClipState:
        new = ClipState(value)
        _guard_state(self, self.state, new, CLIP_TRANSITIONS, frozenset({ClipState.RENDERED}))
        return new


class ApprovalRow(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id"), index=True)
    decision: Mapped[ApprovalDecision] = mapped_column(_enum(ApprovalDecision, "approval_decision"))
    reason: Mapped[RejectReason | None] = mapped_column(_enum(RejectReason, "reject_reason"))
    actor: Mapped[str] = mapped_column(String(16), default="operator")
    decided_at: Mapped[datetime] = mapped_column(default=utcnow)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)


class EveningRow(Base):
    __tablename__ = "evenings"

    date: Mapped[date] = mapped_column(Date, primary_key=True)
    armed_at: Mapped[datetime | None] = mapped_column()
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    slots_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    arm_message_id: Mapped[int | None] = mapped_column(Integer)


class PostRow(Base):
    __tablename__ = "posts"
    # Invariant 7: one post per clip per platform; the same clip file never goes to a platform twice.
    __table_args__ = (UniqueConstraint("clip_id", "platform"), UniqueConstraint("clip_sha256", "platform"))

    id: Mapped[int] = mapped_column(primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id"), index=True)
    clip_sha256: Mapped[str] = mapped_column(String(64))
    platform: Mapped[Platform] = mapped_column(_enum(Platform, "platform"))
    slot_at: Mapped[datetime | None] = mapped_column()
    request_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    upload_post_job: Mapped[str | None] = mapped_column(String(128))
    post_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column()
    state: Mapped[PostState] = mapped_column(_enum(PostState, "post_state"), default=PostState.QUEUED)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Views 24 h and 72 h after publishing (migration 0003), for moment-ranker calibration; filled later.
    views_24h: Mapped[int | None] = mapped_column(Integer)
    views_72h: Mapped[int | None] = mapped_column(Integer)

    @validates("state")
    def _check_state(self, _key: str, value: PostState | str) -> PostState:
        new = PostState(value)
        _guard_state(self, self.state, new, POST_TRANSITIONS, frozenset({PostState.QUEUED}))
        return new


class SubmissionRow(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Invariant 7: exactly one submission per post (so a rejected post can never be resubmitted).
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), unique=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    deadline_at: Mapped[datetime] = mapped_column()
    state: Mapped[SubmissionState] = mapped_column(
        _enum(SubmissionState, "submission_state"), default=SubmissionState.AWAITING
    )
    submitted_at: Mapped[datetime | None] = mapped_column()
    brand_decision: Mapped[BrandDecision] = mapped_column(
        _enum(BrandDecision, "brand_decision"), default=BrandDecision.PENDING
    )
    decided_at: Mapped[datetime | None] = mapped_column()
    paid_usd: Mapped[float] = mapped_column(Float, default=0.0)
    notice_sent_at: Mapped[datetime | None] = mapped_column()
    reminder_sent_at: Mapped[datetime | None] = mapped_column()
    note: Mapped[str] = mapped_column(Text, default="")

    @validates("state")
    def _check_state(self, _key: str, value: SubmissionState | str) -> SubmissionState:
        new = SubmissionState(value)
        _guard_state(self, self.state, new, SUBMISSION_TRANSITIONS, frozenset({SubmissionState.AWAITING}))
        return new


class ViewSnapshotRow(Base):
    __tablename__ = "view_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(default=utcnow)
    views: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16))  # youtube_api | manual


class FewshotRow(Base):
    __tablename__ = "fewshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # positive | negative
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    hook: Mapped[str | None] = mapped_column(String(120))
    transcript_excerpt: Mapped[str] = mapped_column(Text)
    outcome_note: Mapped[str] = mapped_column(Text, default="")
    week: Mapped[str] = mapped_column(String(10))  # ISO week, e.g. 2026-W48


class CostRow(Base):
    __tablename__ = "costs"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    service: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    units: Mapped[float] = mapped_column(Float)
    unit_kind: Mapped[str] = mapped_column(String(32))
    usd: Mapped[float] = mapped_column(Float)


class EventRow(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(16), default="system")  # system | operator
    detail: Mapped[str] = mapped_column(Text, default="")


class FlagRow(Base):
    """Small key/value store for process-wide switches (e.g. the spend-cap pause)."""

    __tablename__ = "flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
