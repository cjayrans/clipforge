"""Telegram review bot (python-telegram-bot v21+, long polling). Docs: docs/ARCHITECTURE.md §4.6, PRD F7.

Security: every update is checked against `TELEGRAM_OWNER_CHAT_ID`. A group-(-1) gate stops all other chats before
any handler runs (they get no reply), and each handler repeats the check (`owner_only`). With no owner configured the
bot refuses to start. Handlers are thin: DB actions live in `review.service` / `review.evening`.

Background work (asyncio tasks; no JobQueue, whose extra is not a declared dependency): preview sender, alert
delivery, heartbeat file every 60 s (the container healthcheck reads it) and the worker-heartbeat watchdog.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import wraps

from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from clipforge import heartbeat
from clipforge.config import Settings
from clipforge.db import make_engine, make_session_factory, session_scope
from clipforge.db.migrate import upgrade
from clipforge.review import keyboards
from clipforge.review.deliver import check_worker_heartbeat, deliver_pending_alerts
from clipforge.review.evening import arm_evening, mark_missed, mark_submitted, skip_from_button
from clipforge.review.notify import Notifier, notifier_from_settings
from clipforge.review.previews import send_pending_previews
from clipforge.review.service import (
    ReviewHooks,
    StaleAction,
    approve_clip,
    reject_clip,
    skip_tonight,
)
from clipforge.review.views import costs_text, queue_text, status_text
from clipforge.schemas import BrandDecision, RejectReason
from clipforge.track.reconcile import (
    ReconcileError,
    apply_amount_reply,
    choose_decision,
    clear_pending_amount,
    next_open_submission,
    reconcile_prompt,
)

log = logging.getLogger(__name__)

TICK_S = 15.0
HEARTBEAT_EVERY_S = 60.0

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def is_owner_chat(chat_id: int | None, owner_chat_id: int | None) -> bool:
    """True only for the configured owner chat. No owner configured means nobody is the owner."""
    return chat_id is not None and owner_chat_id is not None and chat_id == owner_chat_id


@dataclass
class BotDeps:
    settings: Settings
    factory: sessionmaker[Session]
    notifier: Notifier
    hooks: ReviewHooks = field(default_factory=ReviewHooks)
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


@dataclass
class Outcome:
    """What a button press did: a toast, then optionally a new keyboard and/or a follow-up message."""

    toast: str
    markup: InlineKeyboardMarkup | None = None
    clear_keyboard: bool = True
    reply: str | None = None
    reply_markup: InlineKeyboardMarkup | None = None


ActionHandler = Callable[[BotDeps, list[str], int | None], Awaitable[Outcome]]


def _deps(context: ContextTypes.DEFAULT_TYPE) -> BotDeps:
    deps = context.application.bot_data["deps"]
    assert isinstance(deps, BotDeps)
    return deps


def _from_owner(update: Update, deps: BotDeps) -> bool:
    chat = update.effective_chat
    return is_owner_chat(chat.id if chat else None, deps.settings.secrets.telegram_owner_chat_id)


def owner_only(handler: Handler) -> Handler:
    @wraps(handler)
    async def guarded(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _from_owner(update, _deps(context)):
            return
        await handler(update, context)

    return guarded


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Group -1: stop every update that is not from the owner chat before any other handler sees it."""
    if not _from_owner(update, _deps(context)):
        log.info("ignored update from a non-owner chat")
        raise ApplicationHandlerStop


# --------------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------------


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text)


@owner_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, "/status  /queue  /skip  /costs  /reconcile")


@owner_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    with session_scope(deps.factory) as session:
        text = status_text(session, deps.settings, deps.clock())
    await _reply(update, text)


@owner_only
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    with session_scope(deps.factory) as session:
        text = queue_text(session, deps.settings, deps.clock())
    await _reply(update, text)


@owner_only
async def cmd_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    with session_scope(deps.factory) as session:
        text = costs_text(session, deps.settings, deps.clock())
    await _reply(update, text)


@owner_only
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    try:
        with session_scope(deps.factory) as session:
            skip_tonight(session, deps.settings, deps.clock())
    except StaleAction as exc:
        await _reply(update, str(exc))
        return
    await _reply(update, "Tonight is skipped. Nothing will publish; approved clips stay queued for the next evening.")


# --------------------------------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------------------------------


async def _approve(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    clip_id = keyboards.int_part(parts)
    stamp = keyboards.int_part(parts, 1)
    with session_scope(deps.factory) as session:
        approve_clip(session, clip_id, stamp=stamp, now=deps.clock(), message_id=message_id)
    return Outcome("Approved", reply=f"Clip {clip_id}: approved. It joins tonight's queue once you arm the evening.")


async def _reject_menu(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    clip_id = keyboards.int_part(parts)
    stamp = keyboards.int_part(parts, 1)
    return Outcome("Why?", markup=keyboards.reject_reason_keyboard(clip_id, stamp), clear_keyboard=False)


async def _reject(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    clip_id = keyboards.int_part(parts)
    stamp = keyboards.int_part(parts, 1)
    try:
        reason = RejectReason(parts[2])
    except (IndexError, ValueError) as exc:
        raise ValueError("bad reject reason") from exc
    with session_scope(deps.factory) as session:
        reject_clip(session, clip_id, reason, stamp=stamp, now=deps.clock(), message_id=message_id)
    return Outcome("Rejected", reply=f"Clip {clip_id}: rejected ({reason.value}).")


async def _new_hook(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    clip_id = keyboards.int_part(parts)
    return Outcome("Working...", clear_keyboard=False, reply=await deps.hooks.new_hook(clip_id))


async def _shorter(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    clip_id = keyboards.int_part(parts)
    return Outcome("Working...", clear_keyboard=False, reply=await deps.hooks.shorter(clip_id))


async def _arm(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    day = date.fromisoformat(parts[0])  # ValueError -> "Unknown button"
    with session_scope(deps.factory) as session:
        arm_evening(session, deps.settings, deps.clock(), day=day)
        queue = queue_text(session, deps.settings, deps.clock())
    return Outcome("Armed", reply=f"Armed for tonight. Slots publish on time.\n{queue}")


async def _skip(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    day = date.fromisoformat(parts[0])
    with session_scope(deps.factory) as session:
        skip_from_button(session, deps.settings, deps.clock(), day=day)
    return Outcome("Skipped", reply="Tonight is skipped. Nothing will publish.")


async def _submitted(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    with session_scope(deps.factory) as session:
        sub = mark_submitted(session, keyboards.int_part(parts), deps.clock())
        late = " (after the deadline)" if "after the deadline" in sub.note else ""
    return Outcome("Marked submitted", reply=f"Submission {sub.id}: submitted{late}.")


async def _missed(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    with session_scope(deps.factory) as session:
        sub = mark_missed(session, keyboards.int_part(parts), deps.clock())
    return Outcome("Marked missed", reply=f"Submission {sub.id}: marked missed.")


ACTION_HANDLERS: dict[str, ActionHandler] = {
    keyboards.APPROVE: _approve,
    keyboards.REJECT: _reject_menu,
    keyboards.REJECT_REASON: _reject,
    keyboards.NEW_HOOK: _new_hook,
    keyboards.SHORTER: _shorter,
    keyboards.ARM: _arm,
    keyboards.SKIP: _skip,
    keyboards.SUBMITTED: _submitted,
    keyboards.MISSED: _missed,
}


@owner_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    query = update.callback_query
    if query is None:
        return
    try:
        action, parts = keyboards.decode(query.data or "")
        handler = ACTION_HANDLERS[action]
    except (ValueError, KeyError):
        await query.answer("Unknown button", show_alert=True)
        return
    message_id = query.message.message_id if query.message is not None else None
    try:
        outcome = await handler(deps, parts, message_id)
    except StaleAction as exc:
        await query.answer(str(exc)[:200], show_alert=True)
        return
    except ReconcileError as exc:
        await query.answer(exc.message[:200], show_alert=True)
        return
    except ValueError:
        await query.answer("Unknown button", show_alert=True)
        return
    await query.answer(outcome.toast)
    if outcome.markup is not None or outcome.clear_keyboard:
        with contextlib.suppress(Exception):  # message may be too old to edit; the action already happened
            await query.edit_message_reply_markup(reply_markup=outcome.markup)
    if outcome.reply and query.message is not None:
        if outcome.reply_markup is not None:
            await query.message.reply_text(outcome.reply, reply_markup=outcome.reply_markup)
        else:
            await query.message.reply_text(outcome.reply)


# --------------------------------------------------------------------------------------------------
# /reconcile (PRD §4.4): walk the open submissions, oldest first
# --------------------------------------------------------------------------------------------------


def _next_prompt(deps: BotDeps) -> tuple[str, InlineKeyboardMarkup | None]:
    with session_scope(deps.factory) as session:
        sub = next_open_submission(session)
        if sub is None:
            return "Nothing left to reconcile.", None
        return reconcile_prompt(session, sub, deps.settings), keyboards.reconcile_keyboard(sub.id)


@owner_only
async def cmd_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    deps = _deps(context)
    with session_scope(deps.factory) as session:
        clear_pending_amount(session)  # a fresh walk drops an unanswered amount question
    text, markup = _next_prompt(deps)
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def _reconcile(deps: BotDeps, parts: list[str], message_id: int | None) -> Outcome:
    sub_id = keyboards.int_part(parts)
    try:
        decision = BrandDecision(parts[1])
    except (IndexError, ValueError) as exc:
        raise ValueError("bad decision") from exc
    with session_scope(deps.factory) as session:
        needs_amount = choose_decision(session, sub_id, decision, deps.clock())
    if needs_amount:
        return Outcome(decision.value.capitalize(), reply="Reply with the total $ amount paid so far, or 0.")
    text, markup = _next_prompt(deps)
    return Outcome(decision.value.capitalize(), reply=text, reply_markup=markup)


ACTION_HANDLERS[keyboards.RECONCILE] = _reconcile


@owner_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message from the owner is the answer to an open "$ amount" question, if there is one."""
    deps = _deps(context)
    message = update.effective_message
    text = getattr(message, "text", None)
    if message is None or not text:
        return
    try:
        with session_scope(deps.factory) as session:
            sub = apply_amount_reply(session, text, deps.clock())
            saved = None if sub is None else f"Saved: #{sub.id} {sub.brand_decision.value}, paid ${sub.paid_usd:.2f}."
    except ValueError:
        await message.reply_text("Send the amount as a number, like 12.50 or 0.")
        return
    except ReconcileError as exc:
        await message.reply_text(exc.message)
        return
    if saved is None:
        return  # no open question: ignore chatter
    prompt, markup = _next_prompt(deps)
    await message.reply_text(f"{saved}\n\n{prompt}", reply_markup=markup)


# --------------------------------------------------------------------------------------------------
# Background tasks and wiring
# --------------------------------------------------------------------------------------------------


async def background_tick(deps: BotDeps, *, beat: bool) -> None:
    """One pass of the bot's periodic work. Each part is isolated: one failure never stops the others."""
    if beat:
        heartbeat.beat(deps.settings.heartbeat_dir, "bot")
    for name, step in (
        ("previews", lambda: send_pending_previews(deps.factory, deps.settings, deps.notifier)),
        ("alerts", lambda: deliver_pending_alerts(deps.factory, deps.notifier)),
    ):
        try:
            await step()
        except Exception:
            log.exception("bot background step %s failed", name)
    try:
        with session_scope(deps.factory) as session:
            check_worker_heartbeat(session, deps.settings, deps.clock())
    except Exception:
        log.exception("bot background step watchdog failed")


async def _background(app: Application) -> None:  # type: ignore[type-arg]
    deps = app.bot_data["deps"]
    last_beat = 0.0
    loop = asyncio.get_running_loop()
    while True:
        now = loop.time()
        beat = now - last_beat >= HEARTBEAT_EVERY_S
        if beat:
            last_beat = now
        await background_tick(deps, beat=beat)
        await asyncio.sleep(TICK_S)


async def _post_init(app: Application) -> None:  # type: ignore[type-arg]
    app.bot_data["background"] = asyncio.create_task(_background(app))


async def _post_shutdown(app: Application) -> None:  # type: ignore[type-arg]
    task = app.bot_data.get("background")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def register_handlers(app: Application) -> None:  # type: ignore[type-arg]
    app.add_handler(TypeHandler(Update, gate), group=-1)
    for name, fn in (
        ("start", cmd_help),
        ("help", cmd_help),
        ("status", cmd_status),
        ("queue", cmd_queue),
        ("costs", cmd_costs),
        ("skip", cmd_skip),
        ("reconcile", cmd_reconcile),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


def build_application(
    settings: Settings,
    *,
    factory: sessionmaker[Session],
    notifier: Notifier | None = None,
    hooks: ReviewHooks | None = None,
) -> Application:  # type: ignore[type-arg]
    token = settings.secrets.telegram_bot_token
    if token is None or settings.secrets.telegram_owner_chat_id is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID must be set to run the bot")
    app = (
        Application.builder()
        .token(token.get_secret_value())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["deps"] = BotDeps(
        settings=settings,
        factory=factory,
        notifier=notifier or notifier_from_settings(settings),
        hooks=hooks or ReviewHooks(),
    )
    register_handlers(app)
    return app


def run_bot(settings: Settings, *, hooks: ReviewHooks | None = None) -> None:
    """`cf bot`: run the bot until stopped."""
    upgrade(settings.database_url)
    engine = make_engine(settings.database_url)
    try:
        app = build_application(settings, factory=make_session_factory(engine), hooks=hooks)
        log.info("bot starting (publish mode %s)", settings.publish_mode.value)
        app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    finally:
        engine.dispose()
