"""Text shown in Telegram: previews, /status, /queue, /costs. Plain text, read-only DB access."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge import costs, heartbeat
from clipforge.compliance import allowed_platforms
from clipforge.config import Settings
from clipforge.db import ClipRow, CostRow, EveningRow, PostRow
from clipforge.publish.queries import campaign_model, lineage
from clipforge.publish.slots import plan_slots
from clipforge.schemas import ClipState, Slot


def preview_caption(session: Session, settings: Settings, clip: ClipRow) -> str:
    """Caption under a review video: campaign, score, hook, platforms, duration; for moment-ranked clips also the
    archetype and the four scores, and a manual-check line when the ranker flagged the content (never auto-rejected).
    """
    lin = lineage(session, clip)
    campaign = campaign_model(lin.campaign)
    account = settings.app.account_sets.get(clip.account_set)
    platforms = allowed_platforms(campaign, account.platforms if account else None)
    names = ", ".join(p.value for p in platforms) or "no platform"
    cand = lin.candidate
    lines = [
        f"Clip {clip.id} - {campaign.name}",
        f"Score {cand.score} | {clip.duration_s:.0f}s | {names}",
        f"Hook: {cand.hook or '(none)'}",
    ]
    if cand.archetype:
        lines.append(
            f"Moment: {cand.archetype} | reaction {cand.score_reaction}, event {cand.score_event}, "
            f"setup {cand.score_setup}, absurdity {cand.score_absurdity}"
        )
    skips = [str(f) for f in cand.skip_flags or []]
    if skips:  # only penalising skip rules reach review; rejecting ones drop the candidate
        lines.append(f"Skip rules (score lowered): {', '.join(skips)}")
    flags = [str(f) for f in cand.review_flags or []]
    if flags:
        lines.append(f"CHECK BEFORE APPROVING: flagged {'; '.join(flags)}")
    return "\n".join(lines)


def _counts(pairs: list[tuple[object, int]]) -> str:
    return ", ".join(f"{getattr(k, 'value', k)}={v}" for k, v in sorted(pairs, key=lambda kv: str(kv[0]))) or "none"


def status_text(session: Session, settings: Settings, now: datetime) -> str:
    day = now.astimezone(settings.app.tz).date()
    evening = session.get(EveningRow, day)
    tonight = (
        "not armed"
        if evening is None
        else "skipped"
        if evening.skipped
        else "armed"
        if evening.armed_at
        else "not armed"
    )
    paused = costs.is_paused(session)
    mtd = costs.month_to_date(session, settings.app, now=now)
    clips = [(s, n) for s, n in session.execute(select(ClipRow.state, func.count()).group_by(ClipRow.state)).all()]
    posts = [(s, n) for s, n in session.execute(select(PostRow.state, func.count()).group_by(PostRow.state)).all()]
    ages = []
    for name in ("worker", "bot"):
        age = heartbeat.age_s(settings.heartbeat_dir, name)
        ages.append(f"{name} {'never' if age is None else f'{age:.0f}s ago'}")
    return "\n".join(
        [
            f"Mode: {settings.publish_mode.value}",
            f"Tonight ({day}): {tonight}",
            f"Pipeline: {'PAUSED - ' + paused if paused else 'running'}",
            f"Spend: ${mtd:.2f} of ${settings.app.monthly_api_cap_usd:.2f}",
            f"Heartbeats: {', '.join(ages)}",
            f"Clips: {_counts(clips)}",
            f"Posts: {_counts(posts)}",
        ]
    )


def queue_text(session: Session, settings: Settings, now: datetime) -> str:
    day = now.astimezone(settings.app.tz).date()
    evening = session.get(EveningRow, day)
    slots = (
        [Slot.model_validate(s) for s in evening.slots_json]
        if evening is not None and evening.slots_json
        else plan_slots(settings.app, day)
    )
    lines = [f"Slots tonight ({day}):"]
    for slot in slots:
        label = "empty"
        if slot.clip_id is not None:
            clip = session.get(ClipRow, slot.clip_id)
            hook = lineage(session, clip).candidate.hook if clip is not None else None
            label = f"clip {slot.clip_id} - {hook or ''}".rstrip(" -")
        lines.append(f"  {slot.at.astimezone(settings.app.tz):%H:%M}  {label}")
    waiting = session.scalar(select(func.count()).select_from(ClipRow).where(ClipRow.state == ClipState.APPROVED)) or 0
    review = (
        session.scalar(select(func.count()).select_from(ClipRow).where(ClipRow.state == ClipState.PENDING_REVIEW)) or 0
    )
    lines.append(f"Approved, waiting for a slot: {waiting}")
    lines.append(f"Waiting for your review: {review}")
    return "\n".join(lines)


def costs_text(session: Session, settings: Settings, now: datetime) -> str:
    start = costs.month_start_utc(now, settings.app.tz)
    rows = session.execute(
        select(CostRow.service, CostRow.model, func.sum(CostRow.usd))
        .where(CostRow.at >= start)
        .group_by(CostRow.service, CostRow.model)
        .order_by(CostRow.service, CostRow.model)
    ).all()
    lines = [f"{service} {model}: ${usd:.4f}" for service, model, usd in rows] or ["no spend this month"]
    mtd = costs.month_to_date(session, settings.app, now=now)
    lines.append(f"Month to date: ${mtd:.2f} of ${settings.app.monthly_api_cap_usd:.2f}")
    return "\n".join(lines)
