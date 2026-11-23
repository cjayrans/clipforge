"""D2: arm message, arming, submit-now messages, reminders, Submitted / Missed."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import ClipRow, EveningRow, EventRow, PostRow, SubmissionRow, make_session_factory, transition
from clipforge.publish.dispatcher import dispatch_due_slots
from clipforge.review import keyboards
from clipforge.review.bot import BotDeps, on_callback
from clipforge.review.evening import (
    arm_evening,
    arm_time,
    mark_missed,
    mark_submitted,
    reminder_due_at,
    send_arm_message,
    send_reminders,
    send_submission_notices,
)
from clipforge.review.notify import NullNotifier
from clipforge.review.service import StaleAction
from clipforge.schemas import ClipState, Platform, PostState, SubmissionState
from tests.publish.test_dispatcher import approved_clip
from tests.review.fakes import FakeNotifier, callback_data, make_context, make_update
from tests.support.factories import Factory

DAY = date(2026, 11, 23)  # Monday
ARM_AT = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)  # 20:00 ET
FIRST_SLOT = datetime(2026, 11, 24, 1, 10, tzinfo=UTC)  # 20:10 ET


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


# --- arm time ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "local_hhmm", "utc_offset_h"),
    [
        (date(2026, 11, 23), "20:00", -5),  # weekday: first slot 20:10 minus 10 min
        (date(2026, 11, 28), "20:20", -5),  # Saturday: first slot 20:30
        (date(2026, 11, 1), "20:20", -5),  # Sunday, the evening DST ended
        (date(2026, 3, 8), "20:20", -4),  # Sunday, the evening DST started
    ],
)
def test_arm_time_follows_config_and_dst(settings: Settings, day: date, local_hhmm: str, utc_offset_h: int) -> None:
    at = arm_time(settings, day)
    assert at is not None
    assert f"{at.astimezone(settings.app.tz):%H:%M}" == local_hhmm
    assert at.utcoffset() == timedelta(hours=utc_offset_h)


# --- arm message ------------------------------------------------------------------------------------------------


async def test_arm_message_sent_once_in_its_window(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    clip = approved_clip(factory, session)
    assert not await send_arm_message(session, settings, notifier, ARM_AT - timedelta(minutes=1))
    assert await send_arm_message(session, settings, notifier, ARM_AT)
    assert not await send_arm_message(session, settings, notifier, ARM_AT + timedelta(minutes=1))
    (sent,) = notifier.sent
    assert "arm to publish" in sent.text
    assert "20:10" in sent.text
    assert f"clip {clip.id}" in sent.text
    assert "20:40  empty" in sent.text
    assert [keyboards.decode(d)[0] for d in callback_data(sent.keyboard)] == ["arm", "skp"]
    assert callback_data(sent.keyboard)[0] == f"arm:{DAY.isoformat()}"
    evening = session.get(EveningRow, DAY)
    assert evening is not None
    assert evening.arm_message_id == 101
    # showing the plan must not move the clip (assignment happens on arming)
    assert session.get(ClipRow, clip.id).state is ClipState.APPROVED  # type: ignore[union-attr]


async def test_arm_message_not_sent_at_or_after_the_first_slot(
    session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    assert not await send_arm_message(session, settings, notifier, FIRST_SLOT)
    assert notifier.sent == []


async def test_arm_message_not_sent_when_already_skipped_or_armed(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    factory.evening(DAY, skipped=True)
    assert not await send_arm_message(session, settings, notifier, ARM_AT)
    session.get(EveningRow, DAY).skipped = False  # type: ignore[union-attr]
    session.get(EveningRow, DAY).armed_at = ARM_AT  # type: ignore[union-attr]
    assert not await send_arm_message(session, settings, notifier, ARM_AT)
    assert notifier.sent == []


async def test_failed_arm_message_is_retried_next_minute(
    session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    from clipforge.review.notify import NotifyError

    notifier.fail = True
    with pytest.raises(NotifyError):
        await send_arm_message(session, settings, notifier, ARM_AT)
    notifier.fail = False
    assert await send_arm_message(session, settings, notifier, ARM_AT + timedelta(minutes=1))
    assert len(notifier.sent) == 1


async def test_unconfigured_telegram_does_not_resend_every_minute(session: Session, settings: Settings) -> None:
    null = NullNotifier()
    assert await send_arm_message(session, settings, null, ARM_AT)
    assert not await send_arm_message(session, settings, null, ARM_AT + timedelta(minutes=1))


# --- arming ---------------------------------------------------------------------------------------------------------


def test_arm_sets_armed_at_assigns_clips_and_logs_operator_event(
    factory: Factory, session: Session, settings: Settings
) -> None:
    clip = approved_clip(factory, session)
    row = arm_evening(session, settings, ARM_AT + timedelta(minutes=2), day=DAY)
    assert row.armed_at == ARM_AT + timedelta(minutes=2)
    assert session.get(ClipRow, clip.id).state is ClipState.SCHEDULED  # type: ignore[union-attr]
    assert row.slots_json[0]["clip_id"] == clip.id
    event = session.scalars(select(EventRow).where(EventRow.entity == "evening")).one()
    assert event.actor == "operator"
    assert event.to_state == "armed"


def test_arm_refusals(session: Session, settings: Settings) -> None:
    with pytest.raises(StaleAction, match="another evening"):
        arm_evening(session, settings, ARM_AT, day=DAY + timedelta(days=1))
    with pytest.raises(StaleAction, match="first slot"):
        arm_evening(session, settings, FIRST_SLOT)
    arm_evening(session, settings, ARM_AT)
    with pytest.raises(StaleAction, match="already armed"):
        arm_evening(session, settings, ARM_AT + timedelta(minutes=1))


def test_cannot_arm_a_skipped_evening(factory: Factory, session: Session, settings: Settings) -> None:
    factory.evening(DAY, skipped=True)
    with pytest.raises(StaleAction, match="skipped"):
        arm_evening(session, settings, ARM_AT)


# --- the whole evening, through the real dispatcher -----------------------------------------------------------------


async def test_full_evening_dry_run(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    approved_clip(factory, session)
    await send_arm_message(session, settings, notifier, ARM_AT)
    arm_evening(session, settings, ARM_AT + timedelta(minutes=1), day=DAY)
    dispatch_due_slots(session, settings, FIRST_SLOT)

    assert await send_submission_notices(session, settings, notifier, FIRST_SLOT) == 2
    assert await send_submission_notices(session, settings, notifier, FIRST_SLOT) == 0
    notices = notifier.sent[1:]
    assert len(notices) == 2
    text = notices[0].text
    assert text.startswith("SUBMIT NOW (dry run")
    assert "Test Campaign" in text
    assert "https://contentrewards.com/c/test-" in text
    assert "https://example.invalid/" in text
    assert "Deadline: 20:40" in text  # published 20:10 + 30 min
    assert [keyboards.decode(d)[0] for d in callback_data(notices[0].keyboard)] == ["sub", "mis"]
    for sub in session.scalars(select(SubmissionRow)):
        assert sub.notice_sent_at == FIRST_SLOT


async def test_failed_notice_is_retried(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    from clipforge.review.notify import NotifyError

    approved_clip(factory, session)
    arm_evening(session, settings, ARM_AT)
    dispatch_due_slots(session, settings, FIRST_SLOT)
    notifier.fail = True
    with pytest.raises(NotifyError):
        await send_submission_notices(session, settings, notifier, FIRST_SLOT)
    assert all(s.notice_sent_at is None for s in session.scalars(select(SubmissionRow)))
    notifier.fail = False
    assert await send_submission_notices(session, settings, notifier, FIRST_SLOT) == 2


def live_submission(factory: Factory, session: Session, published: datetime) -> SubmissionRow:
    post = factory.post(factory.clip(), Platform.TIKTOK)
    transition(session, post, PostState.PUBLISHING)
    post.post_url = "https://www.tiktok.com/@u/video/1"
    post.published_at = published
    transition(session, post, PostState.LIVE)
    return factory.submission(post, deadline_at=published + timedelta(minutes=30))


async def test_reminder_fires_20_minutes_after_publishing_once(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    sub = live_submission(factory, session, FIRST_SLOT)
    await send_submission_notices(session, settings, notifier, FIRST_SLOT)
    assert reminder_due_at(settings, sub) == FIRST_SLOT + timedelta(minutes=20)
    assert await send_reminders(session, settings, notifier, FIRST_SLOT + timedelta(minutes=19)) == 0
    assert await send_reminders(session, settings, notifier, FIRST_SLOT + timedelta(minutes=20)) == 1
    assert await send_reminders(session, settings, notifier, FIRST_SLOT + timedelta(minutes=21)) == 0
    reminder = notifier.sent[-1]
    assert reminder.text.startswith("Reminder:")
    assert "10 min left" in reminder.text
    assert sub.reminder_sent_at == FIRST_SLOT + timedelta(minutes=20)


async def test_no_reminder_once_marked(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    sub = live_submission(factory, session, FIRST_SLOT)
    await send_submission_notices(session, settings, notifier, FIRST_SLOT)
    mark_submitted(session, sub.id, FIRST_SLOT + timedelta(minutes=5))
    assert await send_reminders(session, settings, notifier, FIRST_SLOT + timedelta(minutes=25)) == 0


async def test_no_reminder_before_the_notice_or_for_fake_posts(
    factory: Factory, session: Session, settings: Settings, notifier: FakeNotifier
) -> None:
    live_submission(factory, session, FIRST_SLOT)
    assert await send_reminders(session, settings, notifier, FIRST_SLOT + timedelta(minutes=25)) == 0  # no notice yet


# --- Submitted / Missed --------------------------------------------------------------------------------------------


def test_mark_submitted_and_missed(factory: Factory, session: Session) -> None:
    a = live_submission(factory, session, FIRST_SLOT)
    b = live_submission(factory, session, FIRST_SLOT)
    mark_submitted(session, a.id, FIRST_SLOT + timedelta(minutes=12))
    assert a.state is SubmissionState.SUBMITTED
    assert a.submitted_at == FIRST_SLOT + timedelta(minutes=12)
    assert a.note == ""
    mark_missed(session, b.id, FIRST_SLOT + timedelta(minutes=40))
    assert b.state is SubmissionState.MISSED
    assert b.submitted_at is None
    for sub in (a, b):
        with pytest.raises(StaleAction, match="already"):
            mark_submitted(session, sub.id, FIRST_SLOT + timedelta(minutes=41))


def test_late_submission_is_recorded_as_late(factory: Factory, session: Session) -> None:
    sub = live_submission(factory, session, FIRST_SLOT)
    mark_submitted(session, sub.id, FIRST_SLOT + timedelta(minutes=31))
    assert sub.state is SubmissionState.SUBMITTED
    assert "after the deadline" in sub.note


def test_unknown_submission_is_stale(session: Session) -> None:
    with pytest.raises(StaleAction, match="no longer exists"):
        mark_missed(session, 404, FIRST_SLOT)


# --- buttons -------------------------------------------------------------------------------------------------------


@pytest.fixture
def deps(settings: Settings, engine: Engine, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch) -> BotDeps:
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "111")
    from clipforge.config import load_settings

    return BotDeps(
        settings=load_settings(),
        factory=make_session_factory(engine),
        notifier=notifier,
        clock=lambda: ARM_AT + timedelta(minutes=1),
    )


async def test_arm_and_skip_buttons(deps: BotDeps, session: Session) -> None:
    arm = make_update(111, keyboards.encode(keyboards.ARM, DAY.isoformat()))
    await on_callback(arm, make_context(deps))
    arm.callback_query.answer.assert_awaited_once_with("Armed")
    session.expire_all()
    row = session.get(EveningRow, DAY)
    assert row is not None
    assert row.armed_at is not None

    again = make_update(111, keyboards.encode(keyboards.ARM, DAY.isoformat()))
    await on_callback(again, make_context(deps))
    assert "already armed" in again.callback_query.answer.await_args.args[0]

    skip = make_update(111, keyboards.encode(keyboards.SKIP, DAY.isoformat()))
    await on_callback(skip, make_context(deps))
    skip.callback_query.answer.assert_awaited_once_with("Skipped")
    session.expire_all()
    assert session.get(EveningRow, DAY).skipped is True  # type: ignore[union-attr]


async def test_old_arm_message_is_stale(deps: BotDeps) -> None:
    update = make_update(111, keyboards.encode(keyboards.ARM, "2026-11-22"))
    await on_callback(update, make_context(deps))
    assert "another evening" in update.callback_query.answer.await_args.args[0]


async def test_bad_date_in_arm_button_is_unknown(deps: BotDeps) -> None:
    update = make_update(111, "arm:not-a-date")
    await on_callback(update, make_context(deps))
    assert update.callback_query.answer.await_args.args[0] == "Unknown button"


async def test_submitted_and_missed_buttons(deps: BotDeps, factory: Factory, session: Session) -> None:
    a = live_submission(factory, session, ARM_AT)
    b = live_submission(factory, session, ARM_AT)
    session.commit()
    ok = make_update(111, keyboards.encode(keyboards.SUBMITTED, a.id))
    await on_callback(ok, make_context(deps))
    ok.callback_query.answer.assert_awaited_once_with("Marked submitted")
    miss = make_update(111, keyboards.encode(keyboards.MISSED, b.id))
    await on_callback(miss, make_context(deps))
    miss.callback_query.answer.assert_awaited_once_with("Marked missed")
    stale = make_update(111, keyboards.encode(keyboards.SUBMITTED, a.id))
    await on_callback(stale, make_context(deps))
    assert "already submitted" in stale.callback_query.answer.await_args.args[0]
    session.expire_all()
    assert session.get(SubmissionRow, a.id).state is SubmissionState.SUBMITTED  # type: ignore[union-attr]
    assert session.get(SubmissionRow, b.id).state is SubmissionState.MISSED  # type: ignore[union-attr]
    assert session.scalars(select(PostRow)).all()
