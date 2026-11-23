"""Test doubles for the Telegram layer (no network)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from telegram import InlineKeyboardMarkup

from clipforge.review.notify import NotifyError


@dataclass
class Sent:
    kind: str  # "text" | "video"
    text: str
    keyboard: InlineKeyboardMarkup | None
    path: Path | None = None


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.fail = False
        self._next_id = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def send_text(self, text: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        if self.fail:
            raise NotifyError("down")
        self.sent.append(Sent("text", text, keyboard))
        return self._id()

    async def send_video(self, path: Path, caption: str, keyboard: InlineKeyboardMarkup | None = None) -> int | None:
        if self.fail:
            raise NotifyError("down")
        self.sent.append(Sent("video", caption, keyboard, path))
        return self._id()

    @property
    def texts(self) -> list[str]:
        return [s.text for s in self.sent]


def callback_data(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [str(b.callback_data) for row in markup.inline_keyboard for b in row]


def make_update(chat_id: int | None, data: str | None = None, message_id: int = 9) -> Any:
    """A minimal Update stand-in: only the attributes the handlers read."""
    message = SimpleNamespace(message_id=message_id, reply_text=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock(), message=message)
    return SimpleNamespace(
        effective_chat=None if chat_id is None else SimpleNamespace(id=chat_id),
        callback_query=query if data is not None else None,
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )


def make_context(deps: Any) -> Any:
    return SimpleNamespace(application=SimpleNamespace(bot_data={"deps": deps}))
