"""`cf post repair <post id>...`: put posts that were marked failed by mistake back on the normal track.

Why it exists: until `interpret_status` was fixed (branch fix/post-status-parsing), a TikTok upload that was still
processing (`success: false` as a placeholder) was recorded as failed although it went on to publish. Such a post is
FAILED in our database, its clip is PUBLISHED, and nothing polls it any more.

What it does, per post, and nothing else:
1. Refuses unless the post is FAILED and was accepted by Upload-Post (it has an `upload_post_job`).
2. Asks Upload-Post for the status (a GET; the only HTTP call) and refuses unless Upload-Post reports the upload as
   COMPLETED. A really failed or still running upload is left alone.
3. With `--apply` only: `db.transition(FAILED -> PUBLISHING, actor="operator")` with an events row that records the old
   error, then the same code the poller uses: LIVE with the URL if there is one, otherwise "published, link not
   released yet" (submission opened from the publish time, link notice sent by the worker).

It never calls `UploadPostClient.publish`, never creates a post, and never touches the clip: invariant 7 (a clip file
goes to a platform once) holds because the same post row and the same Upload-Post job are reused. The default is a dry
run that only reports what would happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ClipRow, PostRow, transition, utcnow
from clipforge.publish.dispatcher import _go_live, _published_awaiting_url
from clipforge.publish.queries import lineage
from clipforge.publish.upload_post import UploadPostClient, UploadPostError, interpret_status
from clipforge.schemas import PostState


@dataclass(frozen=True)
class RepairResult:
    post_id: int
    ok: bool  # True: repaired (apply) or repairable (dry run)
    applied: bool
    message: str


def _refuse(post_id: int, message: str) -> RepairResult:
    return RepairResult(post_id, ok=False, applied=False, message=message)


def repair_failed_posts(
    session: Session,
    settings: Settings,
    post_ids: list[int],
    *,
    apply: bool = False,
    client: UploadPostClient | None = None,
    now: datetime | None = None,
) -> list[RepairResult]:
    now = now or utcnow()
    own_client = client is None
    active = client if client is not None else UploadPostClient.from_settings(settings)
    try:
        return [_repair_one(session, settings, pid, apply=apply, client=active, now=now) for pid in post_ids]
    finally:
        if own_client:
            active.close()


def _repair_one(
    session: Session, settings: Settings, post_id: int, *, apply: bool, client: UploadPostClient, now: datetime
) -> RepairResult:
    post = session.get(PostRow, post_id)
    if post is None:
        return _refuse(post_id, "no such post")
    if post.state is not PostState.FAILED:
        return _refuse(post_id, f"is {post.state.value}; only failed posts are repaired")
    job = post.upload_post_job
    if not job or job.startswith("dry-run-"):
        return _refuse(post_id, "was never accepted by Upload-Post; repairing it would mean sending it again (refused)")
    try:
        status = client.get_status(job)
    except UploadPostError as exc:
        return _refuse(post_id, f"could not read Upload-Post status ({exc}); nothing changed")
    outcome = interpret_status(status, post.platform)
    if outcome.kind == "failed":
        return _refuse(post_id, f"Upload-Post says it really failed ({outcome.error}); nothing changed")
    if outcome.kind == "pending" and not outcome.published_without_url:
        return _refuse(post_id, f"Upload-Post has not completed it (status {status.status}); try again later")

    where = f"URL {outcome.post_url}" if outcome.post_url else "no URL released yet"
    published = outcome.published_at
    when = f"{published:%Y-%m-%d %H:%M} UTC" if published else "time unknown"
    summary = f"Upload-Post reports it completed (published {when}, {where})"
    if not apply:
        return RepairResult(post_id, ok=True, applied=False, message=f"would repair: {summary}")

    old_error = post.error or "no error text"
    transition(
        session,
        post,
        PostState.PUBLISHING,
        actor="operator",
        detail=f"repair: was failed ({old_error[:120]}); {summary}",
    )
    post.error = None
    clip = session.get(ClipRow, post.clip_id)
    assert clip is not None
    campaign_id = lineage(session, clip).campaign.id
    if outcome.kind == "live" and outcome.post_url:
        _go_live(
            session,
            settings,
            post,
            campaign_id,
            outcome.post_url,
            published or now,
            actor="operator",
            detail=f"repair: {outcome.post_url}",
        )
    else:
        _published_awaiting_url(session, settings, post, campaign_id, published, now)
    return RepairResult(post_id, ok=True, applied=True, message=f"repaired: {summary}")
