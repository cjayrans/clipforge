"""`/posturl <post id> <link>`: the operator gives the bot a post's public link by hand.

For a post the platform has PUBLISHED but whose link it has not released yet (TikTok releases the public post ID after
moderation, which can take hours; Content Rewards wants the link within the submission window). The operator copies it
from the app. This only records the link the operator typed: nothing is fetched from the platform, nothing is sent
(no scraping, invariant 8 untouched), and the submission deadline stays counted from the publish time.

The link must be https and on the post's own platform domain, so a typo or a paste of the wrong thing is refused.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ClipRow, PostRow, SubmissionRow
from clipforge.db.transitions import IllegalTransition
from clipforge.publish.dispatcher import _go_live
from clipforge.publish.queries import lineage
from clipforge.review.alerts import redact_urls
from clipforge.schemas import Platform, PostState

MAX_URL_LEN = 500
PLATFORM_DOMAINS: dict[Platform, tuple[str, ...]] = {
    Platform.TIKTOK: ("tiktok.com",),
    Platform.INSTAGRAM: ("instagram.com",),
    Platform.YOUTUBE: ("youtube.com", "youtu.be"),
    Platform.X: ("x.com", "twitter.com"),
}


class PostUrlError(Exception):
    """The link or the post is not acceptable; the message is shown to the operator."""


def validate_post_url(platform: Platform, raw: str) -> str:
    url = raw.strip()
    if not url or len(url) > MAX_URL_LEN or any(ch.isspace() for ch in url):
        raise PostUrlError("That is not a single link. Send /posturl <post id> <link>.")
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        raise PostUrlError("The link must start with https:// (copy it from the app's Share menu).")
    domains = PLATFORM_DOMAINS[platform]
    if not any(host == d or host.endswith("." + d) for d in domains):
        raise PostUrlError(f"That link is not a {platform.value} link (expected {' or '.join(domains)}).")
    if parts.path.strip("/") == "":
        raise PostUrlError("That link points at the site, not at the post.")
    return url


def attach_post_url(session: Session, settings: Settings, post_id: int, raw_url: str, now: datetime) -> PostRow:
    """PUBLISHING (published, no link) -> LIVE with the operator's link; actor `operator`, one events row."""
    post = session.get(PostRow, post_id)
    if post is None:
        raise PostUrlError(f"There is no post {post_id}.")
    if post.state is PostState.LIVE:
        raise PostUrlError(f"Post {post_id} already has its link: {post.post_url}")
    if post.state is PostState.FAILED:
        raise PostUrlError(f"Post {post_id} is marked failed. Run `cf post repair {post_id}` first if it did publish.")
    if post.state is not PostState.PUBLISHING or post.published_at is None:
        raise PostUrlError(f"Post {post_id} is not known to be published yet; wait for its notice.")
    url = validate_post_url(post.platform, raw_url)
    clip = session.get(ClipRow, post.clip_id)
    assert clip is not None
    campaign_id = lineage(session, clip).campaign.id
    try:
        _go_live(
            session,
            settings,
            post,
            campaign_id,
            url,
            post.published_at,
            actor="operator",
            detail=f"link given by the operator: {redact_urls(url)}",
        )
    except IllegalTransition as exc:  # the poller got there first (another process)
        raise PostUrlError(f"Post {post_id} changed while saving the link; check /status and try again.") from exc
    return post


def submission_for(session: Session, post: PostRow) -> SubmissionRow | None:
    return session.scalar(select(SubmissionRow).where(SubmissionRow.post_id == post.id))
