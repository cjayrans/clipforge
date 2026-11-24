"""D3: dispatcher gates (invariants 5, 6, 7, 9, 11), slot assignment, crash safety, status polling.

All Upload-Post HTTP is mocked with respx. The real-HTTP paths run in PRIVATE mode; nothing here uses live mode.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings, load_settings
from clipforge.db import (
    ApprovalRow,
    CandidateRow,
    ClipRow,
    EveningRow,
    EventRow,
    PostRow,
    SubmissionRow,
    transition,
)
from clipforge.publish.dispatcher import (
    GIVE_UP_AFTER,
    IN_PROGRESS_GIVE_UP_AFTER,
    RESEND_AFTER,
    URL_ALERT_AFTER,
    approved_queue,
    dispatch_due_slots,
    poll_pending_posts,
    url_poll_due,
)
from clipforge.publish.repair import repair_failed_posts
from clipforge.publish.upload_post import BASE_URL, UploadPostClient
from clipforge.schemas import ApprovalDecision, ClipState, Platform, PostState, RejectReason
from tests.support.factories import Factory

FIRST_SLOT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)  # Mon 2026-11-23 20:10 America/New_York (EST)
ARMED_AT = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)
DAY = date(2026, 11, 23)


def _settings(monkeypatch: pytest.MonkeyPatch, mode: str) -> Settings:
    monkeypatch.setenv("PUBLISH_MODE", mode)
    return load_settings()


@pytest.fixture
def private_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    return _settings(monkeypatch, "private")


@pytest.fixture
def client() -> UploadPostClient:
    return UploadPostClient("k-test", wait_min_s=0, wait_max_s=0)


@pytest.fixture
def video(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"fake mp4")
    return p


def approved_clip(
    factory: Factory,
    session: Session,
    video: Path | None = None,
    *,
    priority: int = 0,
    minute: int = 5,
    **campaign: object,
) -> ClipRow:
    """A clip approved by the operator after its render (a test fixture against mocked services only)."""
    camp = factory.campaign(priority=priority, **campaign)  # type: ignore[arg-type]
    cand = factory.candidate(
        factory.source(camp),
        captions_json={"tiktok": "heist went sideways", "instagram": "heist went sideways ig", "youtube": "yt"},
        hook="Heist goes wrong",
        title="Heist goes wrong",
    )
    extra = {"path": str(video)} if video else {}
    clip = factory.clip(cand, **extra)
    transition(session, clip, ClipState.PENDING_REVIEW)
    factory.approval(clip, decided_at=clip.rendered_at + timedelta(minutes=minute))
    transition(session, clip, ClipState.APPROVED, actor="operator")
    return clip


def arm(factory: Factory) -> EveningRow:
    return factory.evening(DAY, armed_at=ARMED_AT)


def posts(session: Session, clip: ClipRow | None = None) -> list[PostRow]:
    q = select(PostRow).order_by(PostRow.id)
    if clip is not None:
        q = q.where(PostRow.clip_id == clip.id)
    return list(session.scalars(q))


def alerts_of(session: Session, kind: str) -> list[EventRow]:
    return list(session.scalars(select(EventRow).where(EventRow.entity == "alert", EventRow.to_state == kind)))


# --- dry_run end to end ----------------------------------------------------------------------------------


def test_dry_run_publishes_fake_urls_and_opens_submissions(
    factory: Factory, session: Session, settings: Settings
) -> None:
    clip = approved_clip(factory, session)
    arm(factory)
    results = dispatch_due_slots(session, settings, FIRST_SLOT)

    assert {r.platform for r in results} == {Platform.TIKTOK, Platform.INSTAGRAM}
    rows = posts(session, clip)
    assert len(rows) == 2
    for post in rows:
        assert post.state is PostState.LIVE
        assert post.request_id == str(post.id)
        assert post.post_url == f"https://example.invalid/{post.platform.value}/{post.id}"
        assert post.slot_at == FIRST_SLOT
        sub = session.scalar(select(SubmissionRow).where(SubmissionRow.post_id == post.id))
        assert sub is not None
        assert sub.deadline_at == post.published_at + timedelta(minutes=30)  # type: ignore[operator]
        assert sub.notice_sent_at is None  # the worker's notifier sends the Submit-now message
    assert session.get(ClipRow, clip.id).state is ClipState.PUBLISHED  # type: ignore[union-attr]
    evening = session.get(EveningRow, DAY)
    assert evening is not None
    assert evening.slots_json[0]["clip_id"] == clip.id
    states = [e.to_state for e in session.scalars(select(EventRow).where(EventRow.entity == "post"))]
    assert states.count(str(PostState.PUBLISHING)) == 2
    assert states.count(str(PostState.LIVE)) == 2


def test_second_tick_does_not_publish_again(factory: Factory, session: Session, settings: Settings) -> None:
    approved_clip(factory, session)
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT)
    assert dispatch_due_slots(session, settings, FIRST_SLOT + timedelta(seconds=30)) == []
    assert len(posts(session)) == 2


def test_nothing_due_before_the_lead_window(factory: Factory, session: Session, settings: Settings) -> None:
    approved_clip(factory, session)
    arm(factory)
    assert dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(minutes=5)) == []
    assert posts(session) == []


def test_naive_now_refused(session: Session, settings: Settings) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        dispatch_due_slots(session, settings, datetime(2026, 11, 24, 1, 10))


# --- invariant 6: armed evening ---------------------------------------------------------------------------


def test_unarmed_at_first_slot_skips_the_evening_and_requeues(
    factory: Factory, session: Session, settings: Settings
) -> None:
    clip = approved_clip(factory, session)
    assert dispatch_due_slots(session, settings, FIRST_SLOT) == []
    assert posts(session) == []
    evening = session.get(EveningRow, DAY)
    assert evening is not None
    assert evening.skipped
    assert session.get(ClipRow, clip.id).state is ClipState.APPROVED  # type: ignore[union-attr]
    assert len(alerts_of(session, "evening_skipped")) == 1
    dispatch_due_slots(session, settings, FIRST_SLOT + timedelta(minutes=1))  # idempotent
    assert len(alerts_of(session, "evening_skipped")) == 1


def test_unarmed_before_first_slot_does_nothing_yet(factory: Factory, session: Session, settings: Settings) -> None:
    approved_clip(factory, session)
    assert dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(hours=1)) == []
    assert session.get(EveningRow, DAY) is None


def test_armed_then_skipped_evening_publishes_nothing_and_releases_clips(
    factory: Factory, session: Session, settings: Settings
) -> None:
    clip = approved_clip(factory, session)
    evening = arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(minutes=30))  # assigns the clip (SCHEDULED)
    assert session.get(ClipRow, clip.id).state is ClipState.SCHEDULED  # type: ignore[union-attr]
    from clipforge.publish.dispatcher import skip_evening

    skip_evening(session, evening, reason="operator skipped", actor="operator", alert=False)
    assert dispatch_due_slots(session, settings, FIRST_SLOT) == []
    assert posts(session) == []
    assert session.get(ClipRow, clip.id).state is ClipState.APPROVED  # type: ignore[union-attr]


# --- invariant 5: fresh human approval --------------------------------------------------------------------


def test_stale_approval_is_not_queued(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session, minute=-1)  # approved before the render: stale
    arm(factory)
    assert approved_queue(session, settings) == []
    assert dispatch_due_slots(session, settings, FIRST_SLOT) == []
    assert posts(session, clip) == []
    assert session.get(ClipRow, clip.id).state is ClipState.APPROVED  # type: ignore[union-attr]


def test_reject_after_scheduling_blocks_publish(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session)
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(minutes=30))
    session.add(
        ApprovalRow(
            clip_id=clip.id,
            decision=ApprovalDecision.REJECT,
            reason=RejectReason.BORING,
            actor="operator",
            decided_at=clip.rendered_at + timedelta(minutes=20),
        )
    )
    session.flush()
    assert dispatch_due_slots(session, settings, FIRST_SLOT) == []
    assert posts(session, clip) == []
    assert session.get(ClipRow, clip.id).state is ClipState.APPROVED  # type: ignore[union-attr]
    assert len(alerts_of(session, "gate_refused")) == 1


def test_non_operator_approval_does_not_count(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session)
    for row in session.scalars(select(ApprovalRow).where(ApprovalRow.clip_id == clip.id)):
        row.actor = "system"
    session.flush()
    arm(factory)
    assert approved_queue(session, settings) == []


# --- slots, ordering, lateness ------------------------------------------------------------------------------


def test_one_clip_per_slot_in_priority_then_fifo_order(factory: Factory, session: Session, settings: Settings) -> None:
    low = approved_clip(factory, session, priority=0, minute=1)
    high = approved_clip(factory, session, priority=5, minute=9)
    mid_early = approved_clip(factory, session, priority=0, minute=2)
    extra = approved_clip(factory, session, priority=0, minute=3)
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT)
    # three slots: high, then FIFO among priority 0 (low@1, mid_early@2); extra waits for the next evening.
    assert [p.clip_id for p in posts(session)] == [high.id, high.id]  # only the first slot is due now
    state = {c.id: session.get(ClipRow, c.id).state for c in (low, high, mid_early, extra)}  # type: ignore[union-attr]
    assert state == {
        high.id: ClipState.PUBLISHED,
        low.id: ClipState.SCHEDULED,
        mid_early.id: ClipState.SCHEDULED,
        extra.id: ClipState.APPROVED,
    }
    dispatch_due_slots(session, settings, FIRST_SLOT + timedelta(minutes=15))
    assert posts(session)[-1].clip_id == low.id


def test_late_approval_still_gets_a_slot_on_the_same_evening(
    factory: Factory, session: Session, settings: Settings
) -> None:
    arm(factory)
    assert dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(minutes=10)) == []
    clip = approved_clip(factory, session)  # approved at 20:00-20:10 while the evening is already armed
    results = dispatch_due_slots(session, settings, FIRST_SLOT)
    assert {r.post_id for r in results} == {p.id for p in posts(session, clip)}


def test_slot_missed_beyond_grace_is_not_published_late(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session)
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(minutes=30))  # clip is scheduled into slot 1
    # worker was down: the next tick is 6 minutes after the slot
    assert dispatch_due_slots(session, settings, FIRST_SLOT + timedelta(minutes=6)) == []
    assert posts(session, clip) == []
    assert len(alerts_of(session, "slot_missed")) == 1
    # The clip is re-queued and takes the next slot (20:25 is still ahead).
    assert session.get(ClipRow, clip.id).state is ClipState.SCHEDULED  # type: ignore[union-attr]
    second = FIRST_SLOT + timedelta(minutes=15)
    assert dispatch_due_slots(session, settings, second)
    assert {p.slot_at for p in posts(session, clip)} == {second}


# --- invariants 7 and 9 -------------------------------------------------------------------------------------


def test_political_campaign_never_goes_to_tiktok(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session, is_political=True, allowed_platforms=["tiktok", "instagram", "youtube", "x"])
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT)
    assert [p.platform for p in posts(session, clip)] == [Platform.INSTAGRAM]


def test_platform_outside_the_account_set_is_not_used(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session, allowed_platforms=["tiktok", "instagram", "youtube", "x"])
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT)  # account gaming-1 only enables tiktok and instagram
    assert {p.platform for p in posts(session, clip)} == {Platform.TIKTOK, Platform.INSTAGRAM}


def test_same_file_never_posted_twice_to_a_platform(factory: Factory, session: Session, settings: Settings) -> None:
    first = factory.clip()
    factory.post(first, Platform.TIKTOK, state=PostState.QUEUED)
    twin = approved_clip(factory, session)
    twin.sha256 = first.sha256  # the same file under another clip row
    session.flush()
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT)
    assert [p.platform for p in posts(session, twin)] == [Platform.INSTAGRAM]
    assert alerts_of(session, "gate_refused")


def test_inactive_campaign_clip_expires(factory: Factory, session: Session, settings: Settings) -> None:
    clip = approved_clip(factory, session)
    arm(factory)
    dispatch_due_slots(session, settings, FIRST_SLOT - timedelta(minutes=30))
    from clipforge.db import CampaignRow
    from clipforge.schemas import CampaignStatus

    cand = session.get(CandidateRow, clip.candidate_id)
    assert cand is not None
    campaign_row = session.scalar(select(CampaignRow))
    assert campaign_row is not None
    campaign_row.status = CampaignStatus.DRAINED
    session.flush()
    assert dispatch_due_slots(session, settings, FIRST_SLOT) == []
    assert session.get(ClipRow, clip.id).state is ClipState.EXPIRED  # type: ignore[union-attr]
    assert posts(session) == []


# --- real HTTP paths (PRIVATE mode, respx) ---------------------------------------------------------------------


def test_private_mode_sends_tiktok_only_as_self_only(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/upload").mock(
            return_value=httpx.Response(200, json={"success": True, "request_id": "x"})
        )
        results = dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client)
    assert route.call_count == 1  # Instagram is skipped in private mode
    assert b"SELF_ONLY" in route.calls.last.request.content
    assert b'name="brand_content_toggle"\r\n\r\nfalse' in route.calls.last.request.content
    assert b"paid_partnership" not in route.calls.last.request.content
    assert [r.platform for r in results] == [Platform.TIKTOK]
    (post,) = posts(session, clip)
    assert post.platform is Platform.TIKTOK
    assert post.state is PostState.PUBLISHING
    assert post.upload_post_job == str(post.id)
    assert session.get(ClipRow, clip.id).state is ClipState.PUBLISHED  # type: ignore[union-attr]
    assert session.scalars(select(SubmissionRow)).all() == []  # no submission until there is a URL


def _status(router: respx.MockRouter, body: dict[str, object], code: int = 200) -> respx.Route:
    return router.get(f"{BASE_URL}/uploadposts/status").mock(return_value=httpx.Response(code, json=body))


def _published_private(
    factory: Factory, session: Session, settings: Settings, client: UploadPostClient, video: Path
) -> PostRow:
    approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        dispatch_due_slots(session, settings, FIRST_SLOT, client=client)
    return posts(session)[0]


def test_poll_goes_live_and_opens_submission(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    url = "https://www.tiktok.com/@u/video/1"
    with respx.mock() as router:
        _status(
            router, {"status": "in_progress", "results": [{"platform": "tiktok", "success": True, "message": "Queued"}]}
        )
        assert poll_pending_posts(session, private_settings, FIRST_SLOT + timedelta(minutes=1), client=client) == []
        assert post.state is PostState.PUBLISHING
    with respx.mock() as router:
        _status(
            router,
            {
                "status": "completed",
                "results": [
                    {"platform": "tiktok", "success": True, "post_url": url, "upload_timestamp": "2026-11-24T01:13:00Z"}
                ],
            },
        )
        (res,) = poll_pending_posts(session, private_settings, FIRST_SLOT + timedelta(minutes=3), client=client)
    assert res.post_url == url
    assert post.state is PostState.LIVE
    assert post.post_url == url
    assert post.published_at == datetime(2026, 11, 24, 1, 13, tzinfo=UTC)
    sub = session.scalar(select(SubmissionRow).where(SubmissionRow.post_id == post.id))
    assert sub is not None
    assert sub.deadline_at == datetime(2026, 11, 24, 1, 43, tzinfo=UTC)


def test_poll_failure_and_timeout(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    with respx.mock() as router:
        _status(router, {"status": "pending"})
        poll_pending_posts(
            session, private_settings, post.created_at + GIVE_UP_AFTER - timedelta(seconds=1), client=client
        )
        assert post.state is PostState.PUBLISHING
        poll_pending_posts(
            session, private_settings, post.created_at + GIVE_UP_AFTER + timedelta(seconds=1), client=client
        )
    assert post.state is PostState.FAILED
    assert post.error is not None
    assert "no post URL" in post.error
    assert alerts_of(session, "publish_failed")


def test_poll_failed_status_fails_the_post(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    with respx.mock() as router:
        _status(
            router, {"status": "completed", "results": [{"platform": "tiktok", "success": False, "message": "denied"}]}
        )
        poll_pending_posts(session, private_settings, FIRST_SLOT + timedelta(minutes=1), client=client)
    assert post.state is PostState.FAILED
    assert post.error == "denied"


PUBLISHED_AT = datetime(2026, 11, 24, 1, 12, tzinfo=UTC)  # 20:12 ET, two minutes after the first slot
PROCESSING = {
    "status": "processing",
    "results": [{"platform": "tiktok", "status": "processing", "attempts": 1, "success": False}],
}
COMPLETED_NO_URL = {
    "status": "completed",
    "results": [
        {
            "platform": "tiktok",
            "success": True,
            "post_url": None,
            "upload_timestamp": {"$date": "2026-11-24T01:12:00.000Z"},
            "warnings": ["TikTok published the video but has not released its public post ID yet."],
        }
    ],
}
COMPLETED_WITH_URL = {
    "status": "completed",
    "results": [
        {
            "platform": "tiktok",
            "success": True,
            "post_url": "https://www.tiktok.com/@u/video/777",
            "upload_timestamp": {"$date": "2026-11-24T01:12:00.000Z"},
        }
    ],
}


def _poll(
    session: Session, settings: Settings, client: UploadPostClient, body: dict[str, object], at: datetime
) -> None:
    with respx.mock() as router:
        _status(router, body)
        poll_pending_posts(session, settings, at, client=client)


def test_processing_with_success_false_is_not_a_failure(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    """The bug: a running job reports `success: false`, which was recorded as 'platform reported failure'."""
    post = _published_private(factory, session, private_settings, client, video)
    for minutes in (1, 2, 11, 30):  # well past GIVE_UP_AFTER: Upload-Post still says it is processing
        _poll(session, private_settings, client, PROCESSING, post.created_at + timedelta(minutes=minutes))
        assert post.state is PostState.PUBLISHING
        assert post.error is None
    assert alerts_of(session, "publish_failed") == []
    # ...but a job Upload-Post itself never finishes is eventually given up on, with an alert.
    _poll(
        session,
        private_settings,
        client,
        PROCESSING,
        post.created_at + IN_PROGRESS_GIVE_UP_AFTER + timedelta(minutes=1),
    )
    assert post.state is PostState.FAILED
    assert post.error is not None
    assert "no post URL" in post.error
    assert alerts_of(session, "publish_failed")


def test_completed_without_url_is_published_not_failed(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    _poll(session, private_settings, client, COMPLETED_NO_URL, PUBLISHED_AT + timedelta(seconds=30))
    assert post.state is PostState.PUBLISHING  # still polled, never failed
    assert post.error is None
    assert post.post_url is None
    assert post.published_at == PUBLISHED_AT  # Upload-Post's upload timestamp ({"$date": ...} object)
    sub = session.scalar(select(SubmissionRow).where(SubmissionRow.post_id == post.id))
    assert sub is not None
    assert sub.deadline_at == PUBLISHED_AT + timedelta(minutes=30)  # counts from the publish time
    assert alerts_of(session, "publish_failed") == []
    assert alerts_of(session, "publish_unknown") == []
    marker = [
        e for e in session.scalars(select(EventRow).where(EventRow.entity == "post")) if "published at" in e.detail
    ]
    assert len(marker) == 1
    # More polls change nothing: one submission, same publish time, still no failure.
    _poll(session, private_settings, client, COMPLETED_NO_URL, post.created_at + timedelta(hours=3))
    assert post.state is PostState.PUBLISHING
    assert post.published_at == PUBLISHED_AT
    assert len(session.scalars(select(SubmissionRow)).all()) == 1


def test_a_url_released_later_completes_the_post_and_keeps_the_deadline(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    _poll(session, private_settings, client, COMPLETED_NO_URL, PUBLISHED_AT + timedelta(seconds=30))
    _poll(
        session, private_settings, client, COMPLETED_WITH_URL, datetime(2026, 11, 24, 3, 15, tzinfo=UTC)
    )  # on a 5-min mark
    assert post.state is PostState.LIVE
    assert post.post_url == "https://www.tiktok.com/@u/video/777"
    assert post.published_at == PUBLISHED_AT  # not the time the URL arrived
    (sub,) = session.scalars(select(SubmissionRow)).all()
    assert sub.deadline_at == PUBLISHED_AT + timedelta(minutes=30)
    (ready,) = alerts_of(session, "post_url_ready")
    assert "https://www.tiktok.com/@u/video/777" in ready.detail


def test_alert_only_if_the_url_never_arrives_and_only_once(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    _poll(session, private_settings, client, COMPLETED_NO_URL, PUBLISHED_AT + timedelta(seconds=30))
    # Hour-aligned ticks, so the throttle lets each one through.
    before = datetime(2026, 11, 25, 0, 0, tzinfo=UTC)  # 22h48 after publishing
    assert before - PUBLISHED_AT < URL_ALERT_AFTER
    _poll(session, private_settings, client, COMPLETED_NO_URL, before)
    assert alerts_of(session, "publish_unknown") == []
    for hour in (2, 3, 4):
        _poll(session, private_settings, client, COMPLETED_NO_URL, datetime(2026, 11, 25, hour, 0, tzinfo=UTC))
    (alert,) = alerts_of(session, "publish_unknown")
    assert f"/posturl {post.id}" in alert.detail
    assert post.state is PostState.PUBLISHING  # an alert, not a failure


def test_url_polling_is_throttled_for_published_posts() -> None:
    post = PostRow(published_at=PUBLISHED_AT)
    unknown = PostRow()
    odd = datetime(2026, 11, 24, 1, 20, 17, tzinfo=UTC)
    assert url_poll_due(unknown, odd)  # not yet known to be published: every tick
    assert url_poll_due(post, PUBLISHED_AT + timedelta(minutes=29, seconds=17))  # first 30 min: every tick
    aligned_5min = datetime(2026, 11, 24, 2, 5, tzinfo=UTC)  # 53 min after publishing, on a 5-minute mark
    assert url_poll_due(post, aligned_5min)
    assert not url_poll_due(post, aligned_5min + timedelta(seconds=30))
    assert not url_poll_due(post, aligned_5min + timedelta(minutes=4, seconds=30))
    assert url_poll_due(post, aligned_5min + timedelta(minutes=5))
    after_a_day = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)
    assert url_poll_due(post, after_a_day)  # on the hour
    assert not url_poll_due(post, after_a_day + timedelta(minutes=5))  # overdue: hourly only


def test_failed_status_with_an_error_code_fails_the_post_with_the_code(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _published_private(factory, session, private_settings, client, video)
    body = {
        "status": "failed",
        "results": [
            {
                "platform": "tiktok",
                "status": "failed",
                "success": False,
                "error_code": "tiktok_media_rejected",
                "error_message": "TikTok refused the file",
            }
        ],
    }
    _poll(session, private_settings, client, body, post.created_at + timedelta(minutes=1))
    assert post.state is PostState.FAILED
    assert post.error == "tiktok_media_rejected: TikTok refused the file"
    assert alerts_of(session, "publish_failed")


def test_polling_a_published_post_never_sends_anything_again(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    """Invariant 7: an accepted post is only ever polled (GET); the upload route is never called again."""
    post = _published_private(factory, session, private_settings, client, video)
    with respx.mock(assert_all_called=False) as router:
        status = _status(router, COMPLETED_NO_URL)
        upload = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        for minutes in (1, 5, 40, 120):
            poll_pending_posts(session, private_settings, post.created_at + timedelta(minutes=minutes), client=client)
        assert status.called
        assert not upload.called
    assert len(posts(session)) == 1


# --- repair of posts marked failed by mistake (cf post repair) -------------------------------------------------------


def _wrongly_failed(
    factory: Factory, session: Session, settings: Settings, client: UploadPostClient, video: Path
) -> PostRow:
    post = _published_private(factory, session, settings, client, video)
    assert post.upload_post_job is not None
    transition(session, post, PostState.FAILED, detail="platform reported failure")
    post.error = "platform reported failure"
    session.commit()
    return post


def _repair(
    session: Session,
    settings: Settings,
    client: UploadPostClient,
    post: PostRow,
    body: dict[str, object],
    *,
    apply: bool,
) -> tuple[list, bool]:  # type: ignore[type-arg]
    with respx.mock(assert_all_called=False) as router:
        _status(router, body)
        upload = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        results = repair_failed_posts(session, settings, [post.id], apply=apply, client=client, now=PUBLISHED_AT)
        return results, upload.called


def test_repair_dry_run_changes_nothing(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _wrongly_failed(factory, session, private_settings, client, video)
    events_before = len(session.scalars(select(EventRow)).all())
    (res,), sent = _repair(session, private_settings, client, post, COMPLETED_NO_URL, apply=False)
    assert res.ok
    assert not res.applied
    assert res.message.startswith("would repair")
    assert not sent
    assert post.state is PostState.FAILED
    assert post.error == "platform reported failure"
    assert len(session.scalars(select(EventRow)).all()) == events_before
    assert session.scalars(select(SubmissionRow)).all() == []


def test_repair_apply_uses_transition_with_operator_actor_and_never_resends(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _wrongly_failed(factory, session, private_settings, client, video)
    job = post.upload_post_job
    (res,), sent = _repair(session, private_settings, client, post, COMPLETED_NO_URL, apply=True)
    assert res.ok
    assert res.applied
    assert not sent  # the upload route was never called
    assert post.state is PostState.PUBLISHING  # the poller finishes it; same row, same Upload-Post job
    assert post.upload_post_job == job
    assert post.error is None
    assert post.published_at == PUBLISHED_AT
    assert len(posts(session)) == 1
    (sub,) = session.scalars(select(SubmissionRow)).all()
    assert sub.deadline_at == PUBLISHED_AT + timedelta(minutes=30)
    event = session.scalars(
        select(EventRow).where(
            EventRow.entity == "post", EventRow.from_state == "failed", EventRow.to_state == "publishing"
        )
    ).one()
    assert event.actor == "operator"
    assert "repair" in event.detail
    assert "platform reported failure" in event.detail  # the old error is kept in the audit row


def test_repair_apply_with_a_url_goes_live(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _wrongly_failed(factory, session, private_settings, client, video)
    (res,), _ = _repair(session, private_settings, client, post, COMPLETED_WITH_URL, apply=True)
    assert res.applied
    assert post.state is PostState.LIVE
    assert post.post_url == "https://www.tiktok.com/@u/video/777"
    states = [
        (e.from_state, e.to_state, e.actor) for e in session.scalars(select(EventRow).where(EventRow.entity == "post"))
    ]
    assert ("failed", "publishing", "operator") in states
    assert ("publishing", "live", "operator") in states


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (PROCESSING, "has not completed"),
        (
            {
                "status": "failed",
                "results": [{"platform": "tiktok", "status": "failed", "error_code": "tiktok_media_rejected"}],
            },
            "really failed",
        ),
    ],
)
def test_repair_refuses_when_upload_post_does_not_say_completed(
    factory: Factory,
    session: Session,
    private_settings: Settings,
    client: UploadPostClient,
    video: Path,
    body: dict[str, object],
    why: str,
) -> None:
    post = _wrongly_failed(factory, session, private_settings, client, video)
    (res,), sent = _repair(session, private_settings, client, post, body, apply=True)
    assert not res.ok
    assert not res.applied
    assert why in res.message
    assert not sent
    assert post.state is PostState.FAILED


def test_repair_refuses_posts_that_are_not_failed_or_were_never_accepted(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    live = _published_private(factory, session, private_settings, client, video)
    (res,), _ = _repair(session, private_settings, client, live, COMPLETED_NO_URL, apply=True)
    assert not res.ok
    assert "only failed posts" in res.message
    assert live.state is PostState.PUBLISHING

    never_sent = factory.post(factory.clip(), Platform.TIKTOK)  # failed before Upload-Post accepted it: no job
    transition(session, never_sent, PostState.FAILED)
    (res,), sent = _repair(session, private_settings, client, never_sent, COMPLETED_NO_URL, apply=True)
    assert not res.ok
    assert "never accepted" in res.message
    assert not sent
    assert never_sent.state is PostState.FAILED

    with respx.mock(assert_all_called=False) as router:
        (missing,) = repair_failed_posts(session, private_settings, [99999], client=client)
        assert not router.calls
    assert not missing.ok


def test_repair_survives_an_unreachable_upload_post(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    post = _wrongly_failed(factory, session, private_settings, client, video)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/uploadposts/status").mock(return_value=httpx.Response(503))
        (res,) = repair_failed_posts(session, private_settings, [post.id], apply=True, client=client)
    assert not res.ok
    assert "could not read" in res.message
    assert post.state is PostState.FAILED


# --- crash safety -----------------------------------------------------------------------------------------------


def test_unreachable_upload_post_is_retried_later_with_same_request_id(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(503))
        assert dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client) == []
    (post,) = posts(session, clip)
    assert post.state is PostState.PUBLISHING
    assert post.upload_post_job is None
    with respx.mock() as router:
        route = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        (res,) = poll_pending_posts(session, private_settings, FIRST_SLOT + RESEND_AFTER, client=client)
    assert res.accepted
    assert post.upload_post_job == str(post.id)
    assert route.calls.last.request.headers["Idempotency-Key"] == str(post.id)
    assert len(posts(session, clip)) == 1


def test_poller_does_not_race_an_upload_that_may_still_be_running(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(503))
        dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        too_soon = FIRST_SLOT + RESEND_AFTER - timedelta(seconds=1)
        assert poll_pending_posts(session, private_settings, too_soon, client=client) == []
    assert route.call_count == 0
    (post,) = posts(session, clip)
    assert post.state is PostState.PUBLISHING  # still waiting, not failed


def test_late_resend_outside_the_operator_window_is_refused(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(503))
        dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client)
    after_window = datetime(2026, 11, 24, 2, 5, tzinfo=UTC)  # 21:05 ET, window ends at 21:00
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        assert poll_pending_posts(session, private_settings, after_window, client=client) == []
    assert route.call_count == 0
    (post,) = posts(session, clip)
    assert post.state is PostState.FAILED
    assert post.error is not None
    assert "#11" in post.error


def test_resend_is_refused_when_the_evening_is_no_longer_armed(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    evening = arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(503))
        dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client)
    evening.skipped = True
    session.flush()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(200, json={"success": True}))
        assert poll_pending_posts(session, private_settings, FIRST_SLOT + RESEND_AFTER, client=client) == []
    assert route.call_count == 0
    (post,) = posts(session, clip)
    assert post.state is PostState.FAILED


def test_unreachable_for_too_long_fails_the_post(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(return_value=httpx.Response(503))
        dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client)
        (post,) = posts(session, clip)
        late = post.created_at + GIVE_UP_AFTER + timedelta(minutes=1)
        # still inside the evening window for the gate re-check
        poll_pending_posts(session, private_settings, late, client=client)
    assert post.state is PostState.FAILED


def test_rejection_by_upload_post_fails_the_post(
    factory: Factory, session: Session, private_settings: Settings, client: UploadPostClient, video: Path
) -> None:
    clip = approved_clip(factory, session, video)
    arm(factory)
    with respx.mock() as router:
        router.post(f"{BASE_URL}/upload").mock(
            return_value=httpx.Response(400, json={"success": False, "message": "bad", "error_code": "x_err"})
        )
        (res,) = dispatch_due_slots(session, private_settings, FIRST_SLOT, client=client)
    assert not res.accepted
    (post,) = posts(session, clip)
    assert post.state is PostState.FAILED
    assert "x_err" in (post.error or "")
