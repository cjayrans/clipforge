"""Outbound Telegram messages, usable from the bot (async) and from worker jobs (`run_sync`).

Both processes may *send* with the same bot token; only the bot process polls for updates. Everything goes to the
single owner chat (`TELEGRAM_OWNER_CHAT_ID`). Text is sent as plain text (no parse_mode), so no escaping can fail.

Telegram facts (https://core.telegram.org/bots/api, checked 2026-11-22): sendMessage text 1-4096 chars; sendVideo
caption 0-1024 chars, bots can send videos up to 50 MB; method URL `https://api.telegram.org/bot<token>/<METHOD>`,
answers `{"ok": ..., "result": ...}`. Not stated in the docs: concurrent sends from two processes (known to work), and
the 64-byte callback_data limit was not confirmed on the page text we could read (we enforce it anyway).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, Protocol, TypeVar

from telegram import Bot, InlineKeyboardMarkup
from telegram.request import HTTPXRequest

from clipforge.config import Settings

log = logging.getLogger(__name__)

MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_VIDEO_BYTES = 50 * 1024 * 1024

T = TypeVar("T")


class NotifyError(RuntimeError):
    """A message could not be sent (network, Telegram error, or Telegram is not configured)."""


class Notifier(Protocol):
    async def send_text(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        """Send to the owner chat; returns the Telegram message id when known."""
        ...

    async def send_video(
        self, path: Path, caption: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> int | None: ...


def clip_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class NullNotifier:
    """Used when Telegram is not configured (dev, CI): logs instead of sending."""

    async def send_text(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        log.info("telegram not configured; message not sent: %s", clip_text(text, 200))
        return None

    async def send_video(self, path: Path, caption: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        log.info("telegram not configured; video not sent: %s", path.name)
        return None


class TelegramNotifier:
    def __init__(self, token: str, chat_id: int, *, api_base_url: str | None = None) -> None:
        self._token = token
        self._chat_id = chat_id
        self._base = api_base_url

    async def _with_bot(self, call: Any) -> Any:
        from telegram.error import TelegramError

        request = HTTPXRequest(connect_timeout=10, read_timeout=60, write_timeout=180, pool_timeout=10)
        bot = (
            Bot(self._token, request=request)
            if self._base is None
            else Bot(self._token, base_url=self._base, request=request)
        )
        try:
            return await call(bot)
        except TelegramError as exc:
            # Never include the token: TelegramError messages do not carry the URL, but be explicit.
            raise NotifyError(f"telegram: {type(exc).__name__}: {str(exc).replace(self._token, '***')}") from None
        finally:
            await request.shutdown()

    async def send_text(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        message = await self._with_bot(
            lambda bot: bot.send_message(chat_id=self._chat_id, text=clip_text(text, MAX_TEXT), reply_markup=keyboard)
        )
        return int(message.message_id)

    async def send_video(self, path: Path, caption: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        async def call(bot: Bot) -> Any:
            with path.open("rb") as fh:
                return await bot.send_video(
                    chat_id=self._chat_id,
                    video=fh,
                    caption=clip_text(caption, MAX_CAPTION),
                    supports_streaming=True,
                    reply_markup=keyboard,
                )

        message = await self._with_bot(call)
        return int(message.message_id)


def notifier_from_settings(settings: Settings) -> Notifier:
    token = settings.secrets.telegram_bot_token
    chat_id = settings.secrets.telegram_owner_chat_id
    if token is None or chat_id is None:
        return NullNotifier()
    return TelegramNotifier(token.get_secret_value(), chat_id)


def run_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    """Run an async notifier call from synchronous worker code. Not for use inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    coro.close()
    raise RuntimeError("run_sync() called from a running event loop; await the notifier instead")
