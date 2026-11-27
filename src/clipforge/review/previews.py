"""Send review previews for PENDING_REVIEW clips (the bot's background task; DB is the only coupling to the pipeline).

A clip needs a preview when no preview was sent for its current `rendered_at` (tracked in `flags` as
`preview_sent:<clip_id>` = rendered_at ISO), so a re-rendered clip (New hook / Shorter) gets a fresh message.
Videos over Telegram's 50 MB bot limit fall back to a 480p proxy next to the clip: `<clip stem>.preview.mp4`
(docs/contract-requests/ops.md). With neither, the operator gets a text-only notice with NO buttons: a clip that cannot
be watched cannot be approved from Telegram.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from clipforge.config import Settings
from clipforge.db import ClipRow, VodProposalRow, session_scope
from clipforge.review import alerts, keyboards
from clipforge.review.notify import MAX_VIDEO_BYTES, Notifier, NotifyError
from clipforge.review.service import get_flag, render_stamp, set_flag
from clipforge.review.views import preview_caption
from clipforge.schemas import ClipState

log = logging.getLogger(__name__)


def preview_flag(clip_id: int) -> str:
    return f"preview_sent:{clip_id}"


def pick_video(path: str) -> tuple[Path | None, str]:
    """The file to send (original, or the 480p proxy when the original is over 50 MB) or (None, reason)."""
    original = Path(path)
    try:
        size = original.stat().st_size
    except FileNotFoundError:
        return None, "clip file is missing"
    if size <= MAX_VIDEO_BYTES:
        return original, ""
    proxy = original.with_name(f"{original.stem}.preview.mp4")
    try:
        if proxy.stat().st_size <= MAX_VIDEO_BYTES:
            return proxy, ""
    except FileNotFoundError:
        pass
    return None, f"clip is {size / 1e6:.0f} MB (over 50 MB) and has no 480p proxy"


def clips_needing_preview(session: Session) -> list[int]:
    ids: list[int] = []
    for clip in session.scalars(select(ClipRow).where(ClipRow.state == ClipState.PENDING_REVIEW).order_by(ClipRow.id)):
        if get_flag(session, preview_flag(clip.id)) != clip.rendered_at.isoformat():
            ids.append(clip.id)
    return ids


async def send_pending_previews(factory: sessionmaker[Session], settings: Settings, notifier: Notifier) -> int:
    """Send one preview per clip that needs it. Returns how many clips were handled."""
    with session_scope(factory) as session:
        todo = clips_needing_preview(session)
    handled = 0
    for clip_id in todo:
        with session_scope(factory) as session:
            clip = session.get(ClipRow, clip_id)
            if clip is None or clip.state is not ClipState.PENDING_REVIEW:
                continue
            caption = preview_caption(session, settings, clip)
            video, problem = pick_video(clip.path)
            rendered_iso = clip.rendered_at.isoformat()
            stamp = render_stamp(clip.rendered_at)
        try:
            if video is not None:
                message_id = await notifier.send_video(video, caption, keyboards.review_keyboard(clip_id, stamp))
            else:
                message_id = await notifier.send_text(f"{caption}\n\nNo preview: {problem}.")
        except NotifyError as exc:
            log.warning("preview for clip %s not sent (%s); will retry", clip_id, exc)
            continue
        with session_scope(factory) as session:
            clip = session.get(ClipRow, clip_id)
            if clip is None:
                continue
            clip.review_message_id = message_id
            set_flag(session, preview_flag(clip_id), rendered_iso)
            if video is None:
                alerts.raise_alert(session, alerts.PREVIEW_TOO_BIG, clip_id, f"clip {clip_id}: {problem}")
        handled += 1
    return handled


def vod_flag(proposal_id: int) -> str:
    return f"vod_proposal_sent:{proposal_id}"


async def send_pending_vod_proposals(factory: sessionmaker[Session], notifier: Notifier) -> int:
    """moments-v2: send each pending VOD proposal once, with Add / Dismiss buttons. Returns how many were sent."""
    from clipforge.ingest.twitch_helix import proposal_text

    with session_scope(factory) as session:
        rows = session.scalars(
            select(VodProposalRow).where(VodProposalRow.decision == "pending").order_by(VodProposalRow.id)
        ).all()
        todo = [(r.id, proposal_text(r)) for r in rows if get_flag(session, vod_flag(r.id)) is None]
    sent = 0
    for proposal_id, text in todo:
        try:
            message_id = await notifier.send_text(text, keyboards.vod_keyboard(proposal_id))
        except NotifyError as exc:
            log.warning("VOD proposal %s not sent (%s); will retry", proposal_id, exc)
            continue
        with session_scope(factory) as session:
            row = session.get(VodProposalRow, proposal_id)
            if row is not None:
                row.message_id = message_id
            set_flag(session, vod_flag(proposal_id), "sent")
        sent += 1
    return sent
