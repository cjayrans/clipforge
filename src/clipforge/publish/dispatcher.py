"""Evening dispatcher: slot assignment, gated publishing, status polling (docs/ARCHITECTURE.md §4.7, PRD F8/F9).

Entry points (called by the worker every 30 s):
- `dispatch_due_slots(session, settings, now, *, client=None) -> list[PublishResult]`
- `poll_pending_posts(session, settings, now, *, client=None) -> list[PublishResult]`

Gates, in order, for every post (COMPLIANCE.md):
  #6 the evening is armed (else nothing publishes; at the first slot an unarmed evening is marked skipped),
  #5 a fresh operator approval exists (`compliance.assert_human_approved` against `clip.rendered_at`),
  #9 platform allowed for the campaign and the account (`compliance.allowed_platforms`; private mode drops Instagram),
  #7 one post per clip per platform (`compliance.assert_unique_post`, plus DB unique constraints),
  #2 caption normalized to start with '#ad' (inside `upload_post.build_payload`), #4/#8 inside the payload builder.

Crash safety: the post row (request_id = post id) is committed as PUBLISHING *before* the HTTP call. A post left
PUBLISHING without an Upload-Post reference is re-sent by `poll_pending_posts` with the same request id (idempotent
at Upload-Post), but only after the gates are re-checked, and it is failed after `GIVE_UP_AFTER`.

Clips are assigned to slots lazily, only on an armed evening, because approvals continue until just before the first
slot. Assignment moves APPROVED -> SCHEDULED; releasing a clip moves it back to APPROVED (it rolls to the next
evening). All state changes go through `db.transition()`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.compliance import (
    ComplianceError,
    ExistingPost,
    allowed_platforms,
    assert_evening_armed,
    assert_human_approved,
    assert_unique_post,
)
from clipforge.config import AccountSet, Settings
from clipforge.db import ClipRow, EveningRow, PostRow, SubmissionRow, record_event, transition
from clipforge.publish.queries import Lineage, approvals_for, campaign_model, caption_for, lineage
from clipforge.publish.slots import (
    QueuedClip,
    assign_clips,
    due_slots,
    first_slot_at,
    missed_slots,
    plan_slots,
)
from clipforge.publish.upload_post import UploadPostClient, UploadPostError, interpret_status
from clipforge.review import alerts
from clipforge.schemas import (
    Campaign,
    CampaignStatus,
    ClipState,
    Evening,
    Platform,
    PostState,
    PublishMode,
    PublishRequest,
    PublishResult,
    Slot,
)

log = logging.getLogger(__name__)

# Per ARCHITECTURE §4.7: poll for a post URL for up to 10 minutes (TikTok URLs can take ~3 min).
GIVE_UP_AFTER = timedelta(minutes=10)


# --------------------------------------------------------------------------------------------------
# Evening rows
# --------------------------------------------------------------------------------------------------


def _slots(row: EveningRow) -> list[Slot]:
    return [Slot.model_validate(s) for s in (row.slots_json or [])]


def _store_slots(row: EveningRow, slots: list[Slot]) -> None:
    row.slots_json = [s.model_dump(mode="json") for s in sorted(slots, key=lambda s: s.at)]


def evening_model(row: EveningRow | None, day: date, slots: list[Slot] | None = None) -> Evening:
    return Evening(
        date=day,
        armed_at=row.armed_at if row else None,
        skipped=row.skipped if row else False,
        slots=slots if slots is not None else (_slots(row) if row else []),
    )


def get_or_create_evening(session: Session, settings: Settings, day: date) -> EveningRow:
    """The evening row for `day`, with tonight's planned slots stored on it."""
    row = session.get(EveningRow, day)
    if row is None:
        row = EveningRow(date=day, slots_json=[])
        session.add(row)
    if not row.slots_json:
        _store_slots(row, plan_slots(settings.app, day))
    session.flush()
    return row


# --------------------------------------------------------------------------------------------------
# Queue and slot assignment
# --------------------------------------------------------------------------------------------------


def approved_queue(session: Session, settings: Settings) -> list[QueuedClip]:
    """APPROVED clips with a fresh operator approval whose campaign is active, as queue entries."""
    queue: list[QueuedClip] = []
    for clip in session.scalars(select(ClipRow).where(ClipRow.state == ClipState.APPROVED)):
        if clip.account_set not in settings.app.account_sets:
            continue
        lin = lineage(session, clip)
        if lin.campaign.status is not CampaignStatus.ACTIVE:
            continue
        try:
            approval = assert_human_approved(approvals_for(session, clip.id), clip.rendered_at)
        except ComplianceError:
            continue
        queue.append(QueuedClip(clip.id, lin.campaign.priority, approval.decided_at))
    return queue


def fill_slots(session: Session, settings: Settings, evening: EveningRow, now: datetime) -> list[Slot]:
    """Assign newly approved clips to empty future slots (APPROVED -> SCHEDULED). Only for an armed evening."""
    slots = _slots(evening)
    missed = {s.at for s in missed_slots(slots, now)}
    active = [s for s in slots if s.at not in missed]
    before = {s.clip_id for s in active if s.clip_id is not None}
    assigned = assign_clips(active, approved_queue(session, settings))
    for slot in assigned:
        if slot.clip_id is not None and slot.clip_id not in before:
            clip = session.get(ClipRow, slot.clip_id)
            if clip is not None and clip.state is ClipState.APPROVED:
                transition(session, clip, ClipState.SCHEDULED, detail=f"slot {slot.at.isoformat()}")
    merged = assigned + [s for s in slots if s.at in missed]
    _store_slots(evening, merged)
    session.flush()
    return sorted(merged, key=lambda s: s.at)


def _has_posts(session: Session, clip_id: int) -> bool:
    return session.scalar(select(PostRow.id).where(PostRow.clip_id == clip_id).limit(1)) is not None


def _release(session: Session, evening: EveningRow | None, clip: ClipRow, detail: str) -> None:
    """SCHEDULED -> APPROVED (rolls to the next evening) and empty its slot."""
    if clip.state is ClipState.SCHEDULED:
        transition(session, clip, ClipState.APPROVED, detail=detail)
    if evening is not None:
        _store_slots(
            evening, [Slot(at=s.at, clip_id=None if s.clip_id == clip.id else s.clip_id) for s in _slots(evening)]
        )


def skip_evening(
    session: Session,
    evening: EveningRow,
    *,
    reason: str,
    actor: str = "system",
    alert: bool = True,
) -> None:
    """Mark the evening skipped and return its not-yet-published clips to the queue."""
    if evening.skipped:
        return
    was_armed = evening.armed_at is not None
    evening.skipped = True
    for slot in _slots(evening):
        if slot.clip_id is None:
            continue
        clip = session.get(ClipRow, slot.clip_id)
        if clip is not None and clip.state is ClipState.SCHEDULED and not _has_posts(session, clip.id):
            _release(session, evening, clip, f"evening skipped: {reason}")
    record_event(
        session,
        "evening",
        evening.date.isoformat(),
        "armed" if was_armed else "unarmed",
        "skipped",
        actor="operator" if actor == "operator" else "system",
        detail=reason,
    )
    if alert:
        alerts.raise_alert(session, alerts.EVENING_SKIPPED, evening.date.isoformat(), reason)
    session.flush()


# --------------------------------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------------------------------


def _evening_for(session: Session, settings: Settings, at: datetime) -> Evening:
    day = at.astimezone(settings.app.tz).date()
    return evening_model(session.get(EveningRow, day), day)


def assert_may_publish(session: Session, clip: ClipRow, evening: Evening) -> None:
    """Invariants 6 then 5. Raises ComplianceError."""
    assert_evening_armed(evening)
    assert_human_approved(approvals_for(session, clip.id), clip.rendered_at)


def _fail_post(session: Session, post: PostRow, error: str) -> None:
    post.error = error[:1000]
    transition(session, post, PostState.FAILED, detail=error[:300])
    alerts.raise_alert(session, alerts.PUBLISH_FAILED, post.id, f"{post.platform.value} post {post.id}: {error}")


def _go_live(session: Session, settings: Settings, post: PostRow, campaign_id: int, url: str, at: datetime) -> None:
    post.post_url = url
    post.published_at = at
    transition(session, post, PostState.LIVE, detail=url)
    if session.scalar(select(SubmissionRow.id).where(SubmissionRow.post_id == post.id)) is None:
        session.add(
            SubmissionRow(
                post_id=post.id,
                campaign_id=campaign_id,
                deadline_at=at + timedelta(minutes=settings.app.submission_window_minutes),
            )
        )
    session.flush()


def _client_for(settings: Settings, client: UploadPostClient | None) -> UploadPostClient:
    return client if client is not None else UploadPostClient.from_settings(settings)


# --------------------------------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------------------------------


def _send_post(
    session: Session,
    settings: Settings,
    client: UploadPostClient,
    post: PostRow,
    lin: Lineage,
    campaign: Campaign,
    account: AccountSet,
    now: datetime,
) -> PublishResult | None:
    """Send one PUBLISHING post. Returns None when the outcome is still unknown or the post was failed locally."""
    request = PublishRequest(
        post_id=post.id,
        clip_id=lin.clip.id,
        platform=post.platform,
        upload_post_user=account.upload_post_user,
        video_path=lin.clip.path,
        caption=caption_for(lin.candidate, post.platform),
        youtube_title=lin.candidate.title,
        mode=settings.publish_mode,
    )
    try:
        result = client.publish(request, campaign, settings.app)
    except UploadPostError as exc:
        log.warning("post %s: outcome unknown (%s); will retry with the same request id", post.id, exc)
        if now - post.created_at > GIVE_UP_AFTER:
            _fail_post(session, post, f"Upload-Post unreachable for {GIVE_UP_AFTER}: {exc}")
        session.commit()
        return None
    except (ComplianceError, ValueError) as exc:
        _fail_post(session, post, f"refused before sending: {exc}")
        session.commit()
        return None
    if not result.accepted:
        _fail_post(session, post, result.error or "rejected by Upload-Post")
        session.commit()
        return result
    post.upload_post_job = result.upload_post_ref
    if settings.publish_mode is PublishMode.DRY_RUN and result.post_url:
        _go_live(session, settings, post, lin.campaign.id, result.post_url, now)
    session.commit()
    return result


def _account(settings: Settings, clip: ClipRow) -> AccountSet | None:
    return settings.app.account_sets.get(clip.account_set)


def _eligible_platforms(settings: Settings, campaign: Campaign, account: AccountSet) -> list[Platform]:
    platforms = allowed_platforms(campaign, account.platforms)  # invariant 9
    if settings.publish_mode is PublishMode.PRIVATE:
        platforms = [p for p in platforms if p is not Platform.INSTAGRAM]  # PRD F9
    return platforms


def _dispatch_slot(
    session: Session,
    settings: Settings,
    client: UploadPostClient,
    evening: EveningRow,
    slot: Slot,
    now: datetime,
) -> list[PublishResult]:
    clip = session.get(ClipRow, slot.clip_id) if slot.clip_id is not None else None
    if clip is None or clip.state is not ClipState.SCHEDULED:
        return []
    lin = lineage(session, clip)
    campaign = campaign_model(lin.campaign)
    account = _account(settings, clip)
    if campaign.status is not CampaignStatus.ACTIVE or account is None:
        transition(session, clip, ClipState.EXPIRED, detail="campaign inactive or unknown account set")
        _store_slots(
            evening, [Slot(at=s.at, clip_id=None if s.clip_id == clip.id else s.clip_id) for s in _slots(evening)]
        )
        alerts.raise_alert(session, alerts.CLIP_EXPIRED, clip.id, f"clip {clip.id} expired before publishing")
        return []
    try:
        assert_may_publish(session, clip, evening_model(evening, evening.date))
    except ComplianceError as exc:
        _release(session, evening, clip, str(exc))
        alerts.raise_alert(session, alerts.GATE_REFUSED, clip.id, f"clip {clip.id} not published: {exc}")
        return []
    platforms = _eligible_platforms(settings, campaign, account)
    if not platforms:
        transition(session, clip, ClipState.EXPIRED, detail="no eligible platform")
        alerts.raise_alert(session, alerts.CLIP_EXPIRED, clip.id, f"clip {clip.id} has no eligible platform")
        return []

    results: list[PublishResult] = []
    for platform in platforms:
        post = session.scalar(select(PostRow).where(PostRow.clip_id == clip.id, PostRow.platform == platform))
        if post is None:
            existing = [
                ExistingPost(p.clip_id, p.clip_sha256, p.platform)
                for p in session.scalars(select(PostRow).where(PostRow.platform == platform))
            ]
            try:
                assert_unique_post(clip.id, clip.sha256, platform, existing)  # invariant 7
            except ComplianceError as exc:
                alerts.raise_alert(session, alerts.GATE_REFUSED, clip.id, str(exc))
                continue
            post = PostRow(clip_id=clip.id, clip_sha256=clip.sha256, platform=platform, slot_at=slot.at, created_at=now)
            session.add(post)
            session.flush()
            post.request_id = str(post.id)
            transition(session, post, PostState.PUBLISHING, detail=f"slot {slot.at.isoformat()}")
            session.commit()  # durable before the HTTP call (crash safety)
        elif post.state is not PostState.PUBLISHING or post.upload_post_job:
            continue
        result = _send_post(session, settings, client, post, lin, campaign, account, now)
        if result is not None:
            results.append(result)

    posted = {p.platform for p in session.scalars(select(PostRow).where(PostRow.clip_id == clip.id))}
    if posted >= set(platforms) and clip.state is ClipState.SCHEDULED:
        transition(session, clip, ClipState.PUBLISHED, detail=f"{len(posted)} post(s)")
    session.commit()
    return results


def dispatch_due_slots(
    session: Session, settings: Settings, now: datetime, *, client: UploadPostClient | None = None
) -> list[PublishResult]:
    """One dispatcher tick. Publishes the posts of every slot that is due on an armed evening."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    day = now.astimezone(settings.app.tz).date()
    row = session.get(EveningRow, day)
    slots = _slots(row) if row is not None and row.slots_json else plan_slots(settings.app, day)
    first = first_slot_at(slots)
    if first is None:
        return []

    if not evening_model(row, day, slots).armed:  # invariant 6
        if now >= first and not (row is not None and row.skipped):
            skip_evening(
                session,
                get_or_create_evening(session, settings, day),
                reason="not armed by the first slot; clips return to the queue",
            )
            session.commit()
        return []
    row = get_or_create_evening(session, settings, day)  # armed implies a row; make sure its slots are stored

    for slot in missed_slots(_slots(row), now):  # worker was down or slot passed: never publish late
        clip = session.get(ClipRow, slot.clip_id) if slot.clip_id is not None else None
        if clip is not None and clip.state is ClipState.SCHEDULED and not _has_posts(session, clip.id):
            _release(session, row, clip, "slot missed")
            alerts.raise_alert(
                session, alerts.SLOT_MISSED, clip.id, f"slot {slot.at.isoformat()} passed unpublished; clip re-queued"
            )

    fill_slots(session, settings, row, now)
    session.commit()
    results: list[PublishResult] = []
    active_client = _client_for(settings, client)
    for slot in due_slots(_slots(row), now):
        results.extend(_dispatch_slot(session, settings, active_client, row, slot, now))
    session.commit()
    return results


# --------------------------------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------------------------------


def poll_pending_posts(
    session: Session, settings: Settings, now: datetime, *, client: UploadPostClient | None = None
) -> list[PublishResult]:
    """Re-send posts left unsent after a crash and poll the status of accepted posts until they have a URL.

    A post that is still pending `GIVE_UP_AFTER` after it was created is failed with an alert. When Upload-Post
    reports success but never provides a URL, the alert says so: the post may be live and must be found by hand.
    """
    active_client = _client_for(settings, client)
    results: list[PublishResult] = []
    pending = session.scalars(select(PostRow).where(PostRow.state == PostState.PUBLISHING).order_by(PostRow.id)).all()
    for post in pending:
        clip = session.get(ClipRow, post.clip_id)
        if clip is None:
            continue
        lin = lineage(session, clip)
        if post.upload_post_job is None:
            at = post.slot_at or now
            try:
                assert_may_publish(session, clip, _evening_for(session, settings, at))
            except ComplianceError as exc:
                _fail_post(session, post, f"not re-sent: {exc}")
                session.commit()
                continue
            account = _account(settings, clip)
            if account is None:
                _fail_post(session, post, "unknown account set")
                session.commit()
                continue
            result = _send_post(session, settings, active_client, post, lin, campaign_model(lin.campaign), account, now)
            if result is not None:
                results.append(result)
            continue
        if post.upload_post_job.startswith("dry-run-"):
            continue
        try:
            status = active_client.get_status(post.upload_post_job)
        except UploadPostError as exc:
            log.warning("post %s: status poll failed (%s)", post.id, exc)
            continue
        outcome = interpret_status(status, post.platform)
        if outcome.kind == "live" and outcome.post_url:
            at = outcome.published_at or now
            _go_live(session, settings, post, lin.campaign.id, outcome.post_url, at)
            results.append(
                PublishResult(
                    post_id=post.id,
                    platform=post.platform,
                    accepted=True,
                    post_url=outcome.post_url,
                    published_at=at,
                    upload_post_ref=post.upload_post_job,
                )
            )
        elif outcome.kind == "failed":
            _fail_post(session, post, outcome.error or "Upload-Post reported failure")
        elif now - post.created_at > GIVE_UP_AFTER:
            note = (
                "Upload-Post reports success but gave no URL; the post may be live - find it by hand"
                if outcome.published_without_url
                else f"no post URL after {GIVE_UP_AFTER}"
            )
            _fail_post(session, post, str(note))
        session.commit()
    return results
