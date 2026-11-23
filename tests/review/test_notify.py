"""D1: Telegram notifier. The Bot API is mocked with respx (PTB uses httpx, which respx intercepts)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx

from clipforge.config import Settings, load_settings
from clipforge.review import keyboards
from clipforge.review.notify import (
    MAX_CAPTION,
    MAX_TEXT,
    NotifyError,
    NullNotifier,
    TelegramNotifier,
    clip_text,
    notifier_from_settings,
    run_sync,
)

TOKEN = "123:secret-token-value"
API = f"https://api.telegram.org/bot{TOKEN}"


def ok(message_id: int = 5) -> httpx.Response:
    return httpx.Response(
        200,
        json={"ok": True, "result": {"message_id": message_id, "date": 0, "chat": {"id": 7, "type": "private"}}},
    )


async def test_send_text_uses_documented_method_and_keyboard() -> None:
    notifier = TelegramNotifier(TOKEN, 7)
    with respx.mock() as router:
        route = router.post(f"{API}/sendMessage").mock(return_value=ok(11))
        message_id = await notifier.send_text("hello", keyboards.review_keyboard(3, 5))
    assert message_id == 11
    body = route.calls.last.request.content.decode()
    assert "chat_id=7" in body
    assert "text=hello" in body
    assert "reply_markup" in body and "ap%3A3" in body or '"ap:3"' in body  # callback data carried through


async def test_send_text_truncates_to_telegram_limit() -> None:
    notifier = TelegramNotifier(TOKEN, 7)
    with respx.mock() as router:
        route = router.post(f"{API}/sendMessage").mock(return_value=ok())
        await notifier.send_text("x" * 5000)
    from urllib.parse import parse_qs

    text = parse_qs(route.calls.last.request.content.decode())["text"][0]
    assert len(text) == MAX_TEXT
    assert text.endswith("…")


async def test_send_video_is_multipart_with_caption(tmp_path: Path) -> None:
    video = tmp_path / "c.mp4"
    video.write_bytes(b"videobytes")
    notifier = TelegramNotifier(TOKEN, 7)
    with respx.mock() as router:
        route = router.post(f"{API}/sendVideo").mock(return_value=ok(12))
        message_id = await notifier.send_video(video, "cap", keyboards.review_keyboard(3, 5))
    assert message_id == 12
    req = route.calls.last.request
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b"videobytes" in req.content
    assert b'name="caption"' in req.content
    assert b"cap" in req.content


async def test_caption_limit() -> None:
    assert len(clip_text("y" * 2000, MAX_CAPTION)) == MAX_CAPTION
    assert clip_text("short", MAX_CAPTION) == "short"


async def test_telegram_error_becomes_notify_error_without_the_token() -> None:
    notifier = TelegramNotifier(TOKEN, 7)
    with respx.mock() as router:
        router.post(f"{API}/sendMessage").mock(
            return_value=httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: nope"})
        )
        with pytest.raises(NotifyError) as exc:
            await notifier.send_text("hi")
    assert "Nope" in str(exc.value)
    assert "secret-token-value" not in str(exc.value)


async def test_network_error_becomes_notify_error() -> None:
    notifier = TelegramNotifier(TOKEN, 7)
    with respx.mock() as router:
        router.post(f"{API}/sendMessage").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(NotifyError):
            await notifier.send_text("hi")


async def test_null_notifier_sends_nothing() -> None:
    n = NullNotifier()
    with respx.mock(assert_all_called=False) as router:
        assert await n.send_text("x") is None
        assert await n.send_video(Path("x.mp4"), "c") is None
    assert router.calls.call_count == 0


def test_notifier_from_settings(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(notifier_from_settings(settings), NullNotifier)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert isinstance(notifier_from_settings(load_settings()), NullNotifier)  # token without an owner chat
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "7")
    assert isinstance(notifier_from_settings(load_settings()), TelegramNotifier)


def test_run_sync_from_worker_code() -> None:
    async def coro() -> int:
        return 4

    assert run_sync(coro()) == 4


async def test_run_sync_refuses_inside_a_running_loop() -> None:
    async def coro() -> int:
        return 4

    with pytest.raises(RuntimeError, match="running event loop"):
        run_sync(coro())
    await asyncio.sleep(0)
