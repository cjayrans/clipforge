"""D1: bot handlers, owner guard, background tick. Handlers are called directly with minimal Update stand-ins."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from telegram.ext import ApplicationHandlerStop

from clipforge import heartbeat
from clipforge.config import Settings, load_settings
from clipforge.db import ApprovalRow, ClipRow, make_session_factory, session_scope, transition
from clipforge.review import bot, keyboards
from clipforge.review.bot import BotDeps, build_application, gate, is_owner_chat, on_callback
from clipforge.review.service import ReviewHooks, render_stamp
from clipforge.schemas import ApprovalDecision, ClipState, RejectReason
from tests.review.fakes import FakeNotifier, callback_data, make_context, make_update
from tests.support.factories import Factory

OWNER = 111
RENDERED = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 24, 0, 30, tzinfo=UTC)  # 19:30 ET
STAMP = render_stamp(RENDERED)


@pytest.fixture
def bot_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token-for-tests")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", str(OWNER))
    return load_settings()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def deps(bot_settings: Settings, engine: Engine, notifier: FakeNotifier) -> BotDeps:
    return BotDeps(settings=bot_settings, factory=make_session_factory(engine), notifier=notifier, clock=lambda: NOW)


def pending_clip(factory: Factory, session: Session) -> ClipRow:
    clip = factory.clip(rendered_at=RENDERED)
    transition(session, clip, ClipState.PENDING_REVIEW)
    session.commit()
    return clip


def test_is_owner_chat() -> None:
    assert is_owner_chat(111, 111)
    assert not is_owner_chat(222, 111)
    assert not is_owner_chat(111, None)
    assert not is_owner_chat(None, 111)
    assert not is_owner_chat(None, None)


async def test_gate_stops_other_chats_and_passes_the_owner(deps: BotDeps) -> None:
    ctx = make_context(deps)
    with pytest.raises(ApplicationHandlerStop):
        await gate(make_update(222), ctx)
    with pytest.raises(ApplicationHandlerStop):
        await gate(make_update(None), ctx)
    await gate(make_update(OWNER), ctx)  # no exception


async def test_gate_blocks_everyone_when_no_owner_is_configured(settings: Settings, engine: Engine) -> None:
    deps = BotDeps(settings=settings, factory=make_session_factory(engine), notifier=FakeNotifier())
    with pytest.raises(ApplicationHandlerStop):
        await gate(make_update(111), make_context(deps))


async def test_non_owner_button_press_changes_nothing(deps: BotDeps, factory: Factory, session: Session) -> None:
    clip = pending_clip(factory, session)
    update = make_update(222, keyboards.encode(keyboards.APPROVE, clip.id, STAMP))
    await on_callback(update, make_context(deps))
    update.callback_query.answer.assert_not_awaited()  # not even a toast: strangers get no reply
    session.expire_all()
    assert session.get(ClipRow, clip.id).state is ClipState.PENDING_REVIEW  # type: ignore[union-attr]
    assert session.scalars(select(ApprovalRow)).all() == []


@pytest.mark.parametrize("command", [bot.cmd_status, bot.cmd_queue, bot.cmd_costs, bot.cmd_skip, bot.cmd_help])
async def test_non_owner_commands_get_no_reply(command: Any, deps: BotDeps) -> None:
    update = make_update(222)
    await command(update, make_context(deps))
    update.effective_message.reply_text.assert_not_awaited()


async def test_approve_button(deps: BotDeps, factory: Factory, session: Session) -> None:
    clip = pending_clip(factory, session)
    update = make_update(OWNER, keyboards.encode(keyboards.APPROVE, clip.id, STAMP), message_id=55)
    await on_callback(update, make_context(deps))
    update.callback_query.answer.assert_awaited_once_with("Approved")
    update.callback_query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    session.expire_all()
    assert session.get(ClipRow, clip.id).state is ClipState.APPROVED  # type: ignore[union-attr]
    (approval,) = session.scalars(select(ApprovalRow)).all()
    assert approval.decision is ApprovalDecision.APPROVE
    assert approval.actor == "operator"
    assert approval.telegram_message_id == 55
    assert approval.decided_at == NOW


async def test_reject_flow_asks_for_a_reason_then_rejects(deps: BotDeps, factory: Factory, session: Session) -> None:
    clip = pending_clip(factory, session)
    menu = make_update(OWNER, keyboards.encode(keyboards.REJECT, clip.id, STAMP))
    await on_callback(menu, make_context(deps))
    sent_markup = menu.callback_query.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert len(callback_data(sent_markup)) == len(RejectReason)
    session.expire_all()
    assert session.get(ClipRow, clip.id).state is ClipState.PENDING_REVIEW  # type: ignore[union-attr]

    pick = make_update(OWNER, keyboards.encode(keyboards.REJECT_REASON, clip.id, STAMP, "off_brief"))
    await on_callback(pick, make_context(deps))
    session.expire_all()
    assert session.get(ClipRow, clip.id).state is ClipState.REJECTED  # type: ignore[union-attr]
    (approval,) = session.scalars(select(ApprovalRow)).all()
    assert approval.decision is ApprovalDecision.REJECT
    assert approval.reason is RejectReason.OFF_BRIEF


async def test_old_preview_buttons_are_stale_after_a_rerender(
    deps: BotDeps, factory: Factory, session: Session
) -> None:
    clip = pending_clip(factory, session)
    old_approve = keyboards.encode(keyboards.APPROVE, clip.id, STAMP)
    old_reason = keyboards.encode(keyboards.REJECT_REASON, clip.id, STAMP, "boring")
    transition(session, clip, ClipState.RENDERED)
    clip.rendered_at = RENDERED + timedelta(hours=1)  # re-rendered: new preview, new stamp
    transition(session, clip, ClipState.PENDING_REVIEW)
    session.commit()
    for data in (old_approve, old_reason):
        update = make_update(OWNER, data)
        await on_callback(update, make_context(deps))
        args, kwargs = update.callback_query.answer.await_args
        assert "re-rendered" in args[0]
        assert kwargs["show_alert"] is True
    session.expire_all()
    assert session.get(ClipRow, clip.id).state is ClipState.PENDING_REVIEW  # type: ignore[union-attr]
    assert session.scalars(select(ApprovalRow)).all() == []
    fresh = make_update(OWNER, keyboards.encode(keyboards.APPROVE, clip.id, render_stamp(clip.rendered_at)))
    await on_callback(fresh, make_context(deps))
    fresh.callback_query.answer.assert_awaited_once_with("Approved")


async def test_double_tap_shows_a_stale_alert(deps: BotDeps, factory: Factory, session: Session) -> None:
    clip = pending_clip(factory, session)
    data = keyboards.encode(keyboards.APPROVE, clip.id, STAMP)
    await on_callback(make_update(OWNER, data), make_context(deps))
    again = make_update(OWNER, data)
    await on_callback(again, make_context(deps))
    args, kwargs = again.callback_query.answer.await_args
    assert "not waiting for review" in args[0]
    assert kwargs["show_alert"] is True
    session.expire_all()
    assert len(session.scalars(select(ApprovalRow)).all()) == 1


@pytest.mark.parametrize("data", ["garbage", "ap:notanint", "rr:1:5:not_a_reason", "ap:1", "ap:", "zz:1"])
async def test_malformed_buttons_are_refused(deps: BotDeps, data: str) -> None:
    update = make_update(OWNER, data)
    await on_callback(update, make_context(deps))
    args, kwargs = update.callback_query.answer.await_args
    assert args[0] == "Unknown button"
    assert kwargs["show_alert"] is True


async def test_new_hook_and_shorter_call_the_injected_hooks(deps: BotDeps) -> None:
    calls: list[tuple[str, int]] = []

    async def new_hook(clip_id: int) -> str:
        calls.append(("hook", clip_id))
        return "new hook ready"

    async def shorter(clip_id: int) -> str:
        calls.append(("shorter", clip_id))
        return "trimmed"

    deps.hooks = ReviewHooks(new_hook=new_hook, shorter=shorter)
    for action, text in ((keyboards.NEW_HOOK, "new hook ready"), (keyboards.SHORTER, "trimmed")):
        update = make_update(OWNER, keyboards.encode(action, 5))
        await on_callback(update, make_context(deps))
        update.callback_query.message.reply_text.assert_awaited_once_with(text)
        update.callback_query.edit_message_reply_markup.assert_not_awaited()  # keep the buttons
    assert calls == [("hook", 5), ("shorter", 5)]


async def test_default_hooks_are_stubs_that_change_nothing(deps: BotDeps, factory: Factory, session: Session) -> None:
    clip = pending_clip(factory, session)
    update = make_update(OWNER, keyboards.encode(keyboards.NEW_HOOK, clip.id))
    await on_callback(update, make_context(deps))
    assert "not available" in update.callback_query.message.reply_text.await_args.args[0]
    session.expire_all()
    assert session.get(ClipRow, clip.id).state is ClipState.PENDING_REVIEW  # type: ignore[union-attr]


async def test_status_queue_costs_skip_commands(deps: BotDeps) -> None:
    ctx = make_context(deps)
    for command, needle in (
        (bot.cmd_status, "Mode: dry_run"),
        (bot.cmd_queue, "Slots tonight (2026-11-23)"),
        (bot.cmd_costs, "Month to date"),
    ):
        update = make_update(OWNER)
        await command(update, ctx)
        assert needle in update.effective_message.reply_text.await_args.args[0]
    update = make_update(OWNER)
    await bot.cmd_skip(update, ctx)
    assert "skipped" in update.effective_message.reply_text.await_args.args[0]
    again = make_update(OWNER)
    await bot.cmd_skip(again, ctx)
    assert "already skipped" in again.effective_message.reply_text.await_args.args[0]
    status = make_update(OWNER)
    await bot.cmd_status(status, ctx)
    assert "Tonight (2026-11-23): skipped" in status.effective_message.reply_text.await_args.args[0]


async def test_background_tick_beats_and_isolates_failures(
    deps: BotDeps, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("preview step exploded")

    monkeypatch.setattr(bot, "send_pending_previews", boom)
    await bot.background_tick(deps, beat=True)
    assert heartbeat.age_s(deps.settings.heartbeat_dir, "bot") is not None
    assert "preview step exploded" in caplog.text or "previews failed" in caplog.text


async def test_background_tick_delivers_alerts_even_if_previews_fail(
    deps: BotDeps, monkeypatch: pytest.MonkeyPatch, notifier: FakeNotifier
) -> None:
    from clipforge.review import alerts

    with session_scope(deps.factory) as s:
        alerts.raise_alert(s, alerts.JOB_FAILED, "x", "something broke")

    async def boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("nope")

    monkeypatch.setattr(bot, "send_pending_previews", boom)
    await bot.background_tick(deps, beat=False)
    assert any("something broke" in t for t in notifier.texts)


def test_build_application_refuses_without_token_or_owner(settings: Settings, engine: Engine) -> None:
    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN"):
        build_application(settings, factory=make_session_factory(engine))


def test_build_application_registers_a_gate_before_handlers(bot_settings: Settings, engine: Engine) -> None:
    app = build_application(bot_settings, factory=make_session_factory(engine), notifier=FakeNotifier())
    assert -1 in app.handlers
    assert len(app.handlers[-1]) == 1
    assert 0 in app.handlers
    assert app.bot_data["deps"].settings is bot_settings
    # the token is not stored anywhere we log; the Application holds it only inside its bot
    assert "fake-token" not in repr(app.bot_data["deps"])
