"""Operator actions as plain DB functions (no Telegram types), so handlers stay thin and everything is testable.

Every action refuses stale input with `StaleAction` (a double tap, an old message, a re-rendered clip). State changes
go through `db.transition()`; approvals are written with actor "operator" and `decided_at = now`, which is always
later than the clip's `rendered_at`, so a re-render (which sets a newer `rendered_at`) makes older approvals stale
(invariant 5).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ApprovalRow, ClipRow, EveningRow, FlagRow, transition
from clipforge.publish.dispatcher import get_or_create_evening, skip_evening
from clipforge.schemas import ApprovalDecision, ClipState, RejectReason


class StaleAction(Exception):
    """The button/command no longer applies (already handled, clip changed, evening over)."""


def get_flag(session: Session, key: str) -> str | None:
    row = session.get(FlagRow, key)
    return row.value if row is not None else None


def set_flag(session: Session, key: str, value: str) -> None:
    row = session.get(FlagRow, key)
    if row is None:
        session.add(FlagRow(key=key, value=value))
    else:
        row.value = value
    session.flush()


def render_stamp(rendered_at: datetime) -> int:
    """Identifies one render of a clip (its `rendered_at` in whole seconds). Carried in the Approve/Reject buttons."""
    return int(rendered_at.timestamp())


def _pending_clip(session: Session, clip_id: int, now: datetime, stamp: int) -> ClipRow:
    clip = session.get(ClipRow, clip_id)
    if clip is None:
        raise StaleAction(f"clip {clip_id} no longer exists")
    if clip.state is not ClipState.PENDING_REVIEW:
        raise StaleAction(f"clip {clip_id} is {clip.state.value}, not waiting for review")
    # The buttons of an old preview must never approve a newer render the operator has not watched (invariant 5).
    if stamp != render_stamp(clip.rendered_at) or now <= clip.rendered_at:
        raise StaleAction(f"clip {clip_id} was re-rendered after this message; review the new preview")
    return clip


def approve_clip(
    session: Session, clip_id: int, *, stamp: int, now: datetime, message_id: int | None = None
) -> ApprovalRow:
    """The operator approves a PENDING_REVIEW clip: approvals row + PENDING_REVIEW -> APPROVED.

    `stamp` is the `render_stamp` printed in the button the operator pressed; it must match the clip's current render.
    """
    clip = _pending_clip(session, clip_id, now, stamp)
    row = ApprovalRow(
        clip_id=clip.id,
        decision=ApprovalDecision.APPROVE,
        actor="operator",
        decided_at=now,
        telegram_message_id=message_id,
    )
    session.add(row)
    session.flush()
    transition(session, clip, ClipState.APPROVED, actor="operator", detail=f"approval {row.id}")
    return row


def reject_clip(
    session: Session, clip_id: int, reason: RejectReason, *, stamp: int, now: datetime, message_id: int | None = None
) -> ApprovalRow:
    clip = _pending_clip(session, clip_id, now, stamp)
    row = ApprovalRow(
        clip_id=clip.id,
        decision=ApprovalDecision.REJECT,
        reason=reason,
        actor="operator",
        decided_at=now,
        telegram_message_id=message_id,
    )
    session.add(row)
    session.flush()
    transition(session, clip, ClipState.REJECTED, actor="operator", detail=f"rejected: {reason.value}")
    return row


def skip_tonight(session: Session, settings: Settings, now: datetime) -> EveningRow:
    """The operator skips tonight: nothing publishes and scheduled clips return to the queue."""
    row = get_or_create_evening(session, settings, now.astimezone(settings.app.tz).date())
    if row.skipped:
        raise StaleAction("tonight is already skipped")
    skip_evening(session, row, reason="operator skipped tonight", actor="operator", alert=False)
    return row


async def _new_hook_not_wired(clip_id: int) -> str:
    return f"New hook is not available in this process (`cf bot` wires it). Clip {clip_id} is unchanged."


async def _shorter_not_wired(clip_id: int) -> str:
    return f"Shorter is not available in this process (`cf bot` wires it). Clip {clip_id} is unchanged."


@dataclass
class ReviewHooks:
    """New hook / Shorter callbacks (`pipeline.make_review_hooks`): start a re-render and return a status line.

    A re-render must update `clip.rendered_at` and move the clip back through RENDERED -> PENDING_REVIEW, so the old
    approval is stale and the bot sends a fresh preview (docs/contract-requests/ops.md).
    """

    new_hook: Callable[[int], Awaitable[str]] = field(default=_new_hook_not_wired)
    shorter: Callable[[int], Awaitable[str]] = field(default=_shorter_not_wired)
