"""The evening flow (PRD §4.3, docs/ARCHITECTURE.md §4.6): arm message, arming, submit-now messages, reminders,
Submitted / Missed.

Sending functions are async and take a plain session: the worker calls them through `notify.run_sync`, the bot awaits
them. All are idempotent and safe to call every minute: each message is sent once (`evenings.arm_message_id`,
`submissions.notice_sent_at`, `submissions.reminder_sent_at`), and a failed send is simply retried on the next call.
Message timestamps are written only after Telegram accepted the message.

Rules: arming is closed once the evening is skipped or the first slot has arrived (an unarmed evening is skipped at the
first slot, invariant 6). A submission whose deadline passes without a tap stays AWAITING (the operator may have
submitted without tapping); it shows up in /reconcile.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CampaignRow, ClipRow, EveningRow, PostRow, SubmissionRow, record_event, transition
from clipforge.publish.dispatcher import approved_queue, fill_slots, get_or_create_evening
from clipforge.publish.queries import lineage
from clipforge.publish.slots import assign_clips, first_slot_at, plan_slots
from clipforge.review import keyboards
from clipforge.review.notify import Notifier
from clipforge.review.service import StaleAction, skip_tonight
from clipforge.schemas import PostState, Slot, SubmissionNotice, SubmissionState

log = logging.getLogger(__name__)


def local_day(settings: Settings, now: datetime) -> date:
    return now.astimezone(settings.app.tz).date()


# --------------------------------------------------------------------------------------------------
# Arming
# --------------------------------------------------------------------------------------------------


def arm_time(settings: Settings, day: date) -> datetime | None:
    """When the arm message goes out: `arm_message_minutes_before_first_slot` before the first slot."""
    first = first_slot_at(plan_slots(settings.app, day))
    if first is None:
        return None
    return first - timedelta(minutes=settings.app.arm_message_minutes_before_first_slot)


def arm_evening(session: Session, settings: Settings, now: datetime, *, day: date | None = None) -> EveningRow:
    """The operator arms tonight. Nothing publishes before this (invariant 6). Assigns approved clips to slots."""
    today = local_day(settings, now)
    if day is not None and day != today:
        raise StaleAction("that arm message is for another evening")
    row = get_or_create_evening(session, settings, today)
    if row.skipped:
        raise StaleAction("tonight was skipped; arming is closed")
    if row.armed_at is not None:
        raise StaleAction("tonight is already armed")
    first = first_slot_at([Slot.model_validate(s) for s in row.slots_json])
    if first is not None and now >= first:
        raise StaleAction("the first slot has already arrived; tonight will be skipped")
    row.armed_at = now
    record_event(session, "evening", today.isoformat(), "unarmed", "armed", actor="operator", detail="armed tonight")
    fill_slots(session, settings, row, now)
    session.flush()
    return row


def skip_from_button(session: Session, settings: Settings, now: datetime, *, day: date | None = None) -> EveningRow:
    if day is not None and day != local_day(settings, now):
        raise StaleAction("that arm message is for another evening")
    return skip_tonight(session, settings, now)


def arm_message_text(session: Session, settings: Settings, day: date, slots: list[Slot]) -> str:
    tz = settings.app.tz
    lines = [f"Tonight {day:%a %b %d}: arm to publish. Nothing goes out unless you arm."]
    for slot in slots:
        label = "empty (approve a clip and it fills in)"
        if slot.clip_id is not None:
            clip = session.get(ClipRow, slot.clip_id)
            if clip is not None:
                lin = lineage(session, clip)
                label = f"clip {clip.id} - {lin.campaign.name}: {lin.candidate.hook or ''}".rstrip(": ")
        lines.append(f"  {slot.at.astimezone(tz):%H:%M}  {label}")
    return "\n".join(lines)


async def send_arm_message(session: Session, settings: Settings, notifier: Notifier, now: datetime) -> bool:
    """Send the Arm / Skip message once, between its send time and the first slot. Returns True when sent."""
    day = local_day(settings, now)
    start = arm_time(settings, day)
    first = first_slot_at(plan_slots(settings.app, day))
    if start is None or first is None or not start <= now < first:
        return False
    row = session.get(EveningRow, day)
    if row is not None and (row.arm_message_id is not None or row.armed_at is not None or row.skipped):
        return False
    row = get_or_create_evening(session, settings, day)
    # Show which approved clips would take which slot (not stored: assignment happens when armed).
    shown = assign_clips([Slot.model_validate(s) for s in row.slots_json], approved_queue(session, settings))
    text = arm_message_text(session, settings, day, shown)
    message_id = await notifier.send_text(text, keyboards.arm_keyboard(day))
    row.arm_message_id = message_id or 0  # 0 = sent without a Telegram id (not configured): do not resend
    session.flush()
    return True


# --------------------------------------------------------------------------------------------------
# Submit now, reminders, Submitted / Missed
# --------------------------------------------------------------------------------------------------


def _notice(session: Session, sub: SubmissionRow) -> tuple[SubmissionNotice, bool] | None:
    post = session.get(PostRow, sub.post_id)
    campaign = session.get(CampaignRow, sub.campaign_id)
    if post is None or campaign is None or post.post_url is None or post.state is not PostState.LIVE:
        return None
    notice = SubmissionNotice(
        submission_id=sub.id,
        campaign_name=campaign.name,
        submission_url=campaign.submission_url,
        post_url=post.post_url,
        platform=post.platform,
        deadline_at=sub.deadline_at,
    )
    return notice, (urlsplit(post.post_url).hostname or "").endswith(".invalid")


def submit_now_text(notice: SubmissionNotice, settings: Settings, *, dry_run: bool) -> str:
    deadline = notice.deadline_at.astimezone(settings.app.tz)
    head = "SUBMIT NOW" + (" (dry run: fake URL, do not submit)" if dry_run else "")
    return "\n".join(
        [
            head,
            f"{notice.campaign_name} - {notice.platform.value}",
            f"Submit here: {notice.submission_url}",
            f"Post: {notice.post_url}",
            f"Deadline: {deadline:%H:%M} ({settings.app.submission_window_minutes} min after publishing)",
        ]
    )


async def send_submission_notices(session: Session, settings: Settings, notifier: Notifier, now: datetime) -> int:
    """Send the Submit-now message for every live post that has none yet."""
    pending = session.scalars(
        select(SubmissionRow)
        .where(SubmissionRow.state == SubmissionState.AWAITING, SubmissionRow.notice_sent_at.is_(None))
        .order_by(SubmissionRow.id)
    ).all()
    sent = 0
    for sub in pending:
        built = _notice(session, sub)
        if built is None:
            continue
        notice, fake = built
        await notifier.send_text(submit_now_text(notice, settings, dry_run=fake), keyboards.submit_keyboard(sub.id))
        sub.notice_sent_at = now
        session.flush()
        sent += 1
    return sent


def reminder_due_at(settings: Settings, sub: SubmissionRow) -> datetime:
    """deadline - (window - reminder): with the defaults 30 and 20 minutes, 20 minutes after publishing."""
    app = settings.app
    return sub.deadline_at - timedelta(minutes=app.submission_window_minutes - app.submission_reminder_minutes)


async def send_reminders(session: Session, settings: Settings, notifier: Notifier, now: datetime) -> int:
    pending = session.scalars(
        select(SubmissionRow)
        .where(
            SubmissionRow.state == SubmissionState.AWAITING,
            SubmissionRow.notice_sent_at.is_not(None),
            SubmissionRow.reminder_sent_at.is_(None),
        )
        .order_by(SubmissionRow.id)
    ).all()
    sent = 0
    for sub in pending:
        if now < reminder_due_at(settings, sub):
            continue
        built = _notice(session, sub)
        if built is None:
            continue
        notice, fake = built
        if fake:  # dry run: no reminder for a fake post
            sub.reminder_sent_at = now
            continue
        minutes = max(0, int((sub.deadline_at - now).total_seconds() // 60))
        text = f"Reminder: {notice.campaign_name} ({notice.platform.value}) is not marked yet. {minutes} min left."
        await notifier.send_text(text + f"\n{notice.submission_url}", keyboards.submit_keyboard(sub.id))
        sub.reminder_sent_at = now
        session.flush()
        sent += 1
    return sent


def _awaiting(session: Session, submission_id: int) -> SubmissionRow:
    sub = session.get(SubmissionRow, submission_id)
    if sub is None:
        raise StaleAction(f"submission {submission_id} no longer exists")
    if sub.state is not SubmissionState.AWAITING:
        raise StaleAction(f"submission {submission_id} is already {sub.state.value}")
    return sub


def mark_submitted(session: Session, submission_id: int, now: datetime) -> SubmissionRow:
    sub = _awaiting(session, submission_id)
    sub.submitted_at = now
    detail = "submitted"
    if now > sub.deadline_at:
        sub.note = (sub.note + " submitted after the deadline").strip()
        detail = "submitted after the deadline"
    transition(session, sub, SubmissionState.SUBMITTED, actor="operator", detail=detail)
    return sub


def mark_missed(session: Session, submission_id: int, now: datetime) -> SubmissionRow:
    sub = _awaiting(session, submission_id)
    transition(session, sub, SubmissionState.MISSED, actor="operator", detail="marked missed")
    return sub
