"""P2.4 / PRD §7.2: one source -> candidates -> render -> QA -> approve -> arm -> publish (dry_run) -> Submit now ->
Submitted -> reconcile, fully mocked.

Mocks: Groq, the footage link and Upload-Post via respx (Upload-Post must never be called in dry_run); Anthropic via
httpx2.MockTransport; Telegram via FakeNotifier and handler stand-ins; ffmpeg/ffprobe/MediaPipe via injected fakes, so
this runs on the Mac without ffmpeg. `test_e2e_with_real_render` repeats the run with the real encoder (CI, Docker).
"""

from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from clipforge.config import Settings
from clipforge.db import (
    ApprovalRow,
    CandidateRow,
    ClipRow,
    CostRow,
    EventRow,
    PostRow,
    SourceRow,
    SubmissionRow,
    session_scope,
)
from clipforge.pipeline import PipelineDeps, run_once
from clipforge.publish.dispatcher import dispatch_due_slots
from clipforge.review import bot, keyboards
from clipforge.review.bot import BotDeps
from clipforge.review.evening import arm_evening, send_submission_notices
from clipforge.review.previews import send_pending_previews
from clipforge.schemas import BrandDecision, ClipState, PostState, SourceStatus, SubmissionState
from tests.rank.anthropic_mock import FakeAnthropic
from tests.review.fakes import FakeNotifier, callback_data, make_context, make_update
from tests.support.pipeline_fakes import SOURCE_URL
from tests.support.pipeline_fixtures import OWNER, Clock, add_campaign

DAY = date(2026, 11, 23)
APPROVE_AT = datetime(2026, 11, 24, 0, 55, tzinfo=UTC)  # 19:55 ET
ARM_AT = datetime(2026, 11, 24, 1, 1, tzinfo=UTC)  # 20:01 ET
SLOT_AT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)  # 20:10 ET, the first weekday slot
SUBMIT_AT = datetime(2026, 11, 24, 1, 20, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 28, 15, 0, tzinfo=UTC)


def _button(markup: object, action: str) -> str:
    for data in callback_data(markup):  # type: ignore[arg-type]
        if keyboards.decode(data)[0] == action:
            return data
    raise AssertionError(f"no {action} button")


async def _press(deps: BotDeps, data: str) -> SimpleNamespace:
    update = make_update(OWNER, data)
    await bot.on_callback(update, make_context(deps))
    return update  # type: ignore[no-any-return]


async def _reply(deps: BotDeps, text: str) -> str:
    update = make_update(OWNER)
    update.effective_message = SimpleNamespace(text=text, reply_text=AsyncMock())
    await bot.on_text(update, make_context(deps))
    return str(update.effective_message.reply_text.await_args.args[0])


def _events(s: Session, entity: str, entity_id: int) -> list[tuple[str | None, str | None]]:
    rows = s.scalars(
        select(EventRow).where(EventRow.entity == entity, EventRow.entity_id == str(entity_id)).order_by(EventRow.id)
    )
    return [(e.from_state, e.to_state) for e in rows]


async def _full_flow(
    settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    router: respx.MockRouter,
) -> None:
    campaign_id, source_id = add_campaign(db, settings)

    # --- 01:00 nightly run ------------------------------------------------------------------------------
    report = run_once(settings, deps=deps)
    assert report.paused is None, report.summary()
    assert report.problems == [], report.summary()
    assert (report.downloaded, report.transcribed, report.ranked) == (1, 1, 1)
    assert report.ready >= 1
    assert report.ready == report.rendered  # every rendered clip passed QA

    with session_scope(db) as s:
        src = s.get(SourceRow, source_id)
        assert src is not None
        assert src.status is SourceStatus.RANKED
        assert [t for _, t in _events(s, "source", source_id)] == ["registered", "downloaded", "transcribed", "ranked"]
        cands = s.scalars(select(CandidateRow).where(CandidateRow.source_id == source_id)).all()
        assert len(cands) == report.candidates_created
        for c in cands:
            assert c.captions_json is not None
            assert set(c.captions_json) == {"tiktok", "instagram"}  # stored as {platform: text} (ops CR 3)
            assert all(t.startswith("#ad ") and "#GTA6" in t for t in c.captions_json.values())
            assert c.prompt_version == "score_windows@1+clip_copy@1"
        clips = s.scalars(select(ClipRow).order_by(ClipRow.id)).all()
        assert len(clips) == report.ready
        assert all(c.state is ClipState.PENDING_REVIEW for c in clips)
        assert all(c.qa_report_json is not None for c in clips)
        clip = clips[0]
        clip_id = clip.id
        assert _events(s, "clip", clip_id) == [(None, "rendered"), ("rendered", "pending_review")]
        services = {row[0] for row in s.execute(select(CostRow.service, CostRow.model))}
        models = {row[1] for row in s.execute(select(CostRow.service, CostRow.model))}
        assert services == {"groq", "anthropic"}
        assert {"whisper-large-v3-turbo", "claude-haiku-5-5", "claude-sonnet-5-5"} <= models

    # --- the bot sends previews; Carson approves one at 19:55 ------------------------------------------------
    notifier = FakeNotifier()
    assert await send_pending_previews(db, settings, notifier) == report.ready
    videos = [m for m in notifier.sent if m.kind == "video"]
    assert len(videos) == report.ready
    bot_deps = BotDeps(settings=settings, factory=db, notifier=notifier, clock=lambda: APPROVE_AT)
    approve = _button(videos[0].keyboard, keyboards.APPROVE)
    approved_clip = keyboards.int_part(keyboards.decode(approve)[1])
    assert approved_clip == clip_id
    await _press(bot_deps, approve)
    with session_scope(db) as s:
        row = s.get(ClipRow, approved_clip)
        assert row is not None
        assert row.state is ClipState.APPROVED
        approvals = s.scalars(select(ApprovalRow).where(ApprovalRow.clip_id == approved_clip)).all()
        assert [(a.decision.value, a.actor) for a in approvals] == [("approve", "operator")]

    # Unarmed, the due slot publishes nothing (invariant 6; also tests/compliance/test_inv06).
    with session_scope(db) as s:
        assert dispatch_due_slots(s, settings, ARM_AT) == []
        assert s.scalar(select(func.count()).select_from(PostRow)) == 0
    with session_scope(db) as s:
        arm_evening(s, settings, ARM_AT, day=DAY)

    # --- 20:10 slot: dry_run publish to TikTok and Instagram, no HTTP to Upload-Post ---------------------------
    with session_scope(db) as s:
        results = dispatch_due_slots(s, settings, SLOT_AT)
    assert {r.platform.value for r in results} == {"tiktok", "instagram"}
    assert all(r.accepted for r in results)
    assert not router.routes[2].called  # Upload-Post
    with session_scope(db) as s:
        posts = s.scalars(select(PostRow).where(PostRow.clip_id == approved_clip)).all()
        assert {p.state for p in posts} == {PostState.LIVE}
        assert all(p.post_url and p.post_url.startswith("https://example.invalid/") for p in posts)
        row = s.get(ClipRow, approved_clip)
        assert row is not None
        assert row.state is ClipState.PUBLISHED
        subs = s.scalars(select(SubmissionRow)).all()
        assert len(subs) == 2

    # --- Submit now messages, Carson taps Submitted --------------------------------------------------------
    notifier.sent.clear()
    with session_scope(db) as s:
        sent = await send_submission_notices(s, settings, notifier, SLOT_AT)
    assert sent == 2
    notice = notifier.sent[0]
    assert "dry run: fake URL, do not submit" in notice.text
    assert "example.invalid" in notice.text
    submit_deps = BotDeps(settings=settings, factory=db, notifier=notifier, clock=lambda: SUBMIT_AT)
    for msg in notifier.sent:
        await _press(submit_deps, _button(msg.keyboard, keyboards.SUBMITTED))
    with session_scope(db) as s:
        assert {x.state for x in s.scalars(select(SubmissionRow))} == {SubmissionState.SUBMITTED}

    # --- Saturday /reconcile: first Paid $12.50, then Rejected ---------------------------------------------------
    rec_deps = BotDeps(settings=settings, factory=db, notifier=notifier, clock=lambda: SATURDAY)
    with session_scope(db) as s:
        first, second = (x.id for x in s.scalars(select(SubmissionRow).order_by(SubmissionRow.id)))
    await _press(rec_deps, keyboards.encode(keyboards.RECONCILE, first, BrandDecision.PAID.value))
    assert f"Saved: #{first} paid, paid $12.50." in await _reply(rec_deps, "12.50")
    await _press(rec_deps, keyboards.encode(keyboards.RECONCILE, second, BrandDecision.REJECTED.value))
    with session_scope(db) as s:
        a = s.get(SubmissionRow, first)
        b = s.get(SubmissionRow, second)
        assert a is not None
        assert b is not None
        assert (a.brand_decision, a.paid_usd) == (BrandDecision.PAID, 12.50)
        assert b.brand_decision is BrandDecision.REJECTED
        # Every state change of the clip is in the audit log.
        assert [t for _, t in _events(s, "clip", approved_clip)] == [
            "rendered",
            "pending_review",
            "approved",
            "scheduled",
            "published",
        ]
        # Groq once, Haiku per batch, Sonnet per candidate (+ copy failures): all priced.
        assert s.scalar(select(func.count()).select_from(CostRow)) >= 3


async def test_e2e_mocked(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    deps: PipelineDeps,
    anthropic: FakeAnthropic,
    mocked_http: respx.MockRouter,
) -> None:
    await _full_flow(pipe_settings, db, deps, anthropic, mocked_http)
    assert mocked_http.routes[0].call_count == 1  # Groq: one chunk
    assert mocked_http.routes[1].call_count == 1  # footage downloaded once
    assert SOURCE_URL in str(mocked_http.routes[1].calls.last.request.url)


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg/ffprobe")
async def test_e2e_with_real_render(
    pipe_settings: Settings,
    db: sessionmaker[Session],
    anthropic: FakeAnthropic,
    mocked_http: respx.MockRouter,
    clock: Clock,
) -> None:
    """Same flow with the real encoder, ffprobe, PySceneDetect and (when installed) MediaPipe on the 90 s fixture."""
    from tests.conftest import FIXTURES

    mocked_http.routes[1].mock(
        return_value=httpx.Response(
            200, content=(FIXTURES / "synthetic_90s.mp4").read_bytes(), headers={"content-type": "video/mp4"}
        )
    )
    real = PipelineDeps(local_transcriber=None, claude_http_client=anthropic.http_client(), clock=clock)
    await _full_flow(pipe_settings, db, real, anthropic, mocked_http)
    with session_scope(db) as s:
        clip = s.scalars(select(ClipRow).order_by(ClipRow.id)).first()
        assert clip is not None
        assert clip.path.endswith(".mp4")
