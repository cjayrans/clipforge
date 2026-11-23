"""D1: review previews and alert delivery / watchdog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from clipforge import costs, heartbeat
from clipforge.config import Settings
from clipforge.db import ClipRow, EventRow, make_session_factory, session_scope, transition
from clipforge.review import alerts, keyboards, previews
from clipforge.review.deliver import (
    ALERT_CURSOR,
    check_worker_heartbeat,
    deliver_pending_alerts,
    worker_is_stale,
)
from clipforge.review.previews import pick_video, send_pending_previews
from clipforge.review.service import get_flag
from clipforge.schemas import ClipState
from tests.review.fakes import FakeNotifier, callback_data
from tests.support.factories import Factory


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def pending(factory: Factory, session: Session, path: Path, **kw: object) -> ClipRow:
    clip = factory.clip(path=str(path), rendered_at=datetime(2026, 11, 23, 6, 0, tzinfo=UTC), **kw)
    transition(session, clip, ClipState.PENDING_REVIEW)
    session.commit()
    return clip


@pytest.fixture
def video(tmp_path: Path) -> Path:
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"x" * 100)
    return p


# --- previews ------------------------------------------------------------------------------------------------


async def test_preview_sent_once_with_review_buttons(
    factory: Factory, session: Session, engine: Engine, settings: Settings, notifier: FakeNotifier, video: Path
) -> None:
    clip = pending(factory, session, video)
    fac = make_session_factory(engine)
    assert await send_pending_previews(fac, settings, notifier) == 1
    (sent,) = notifier.sent
    assert sent.kind == "video"
    assert sent.path == video
    assert f"Clip {clip.id}" in sent.text
    assert "Hook:" in sent.text
    assert "tiktok" in sent.text
    assert [keyboards.decode(d)[0] for d in callback_data(sent.keyboard)] == ["ap", "rj", "nh", "sh"]
    session.expire_all()
    assert session.get(ClipRow, clip.id).review_message_id == 101  # type: ignore[union-attr]
    assert await send_pending_previews(fac, settings, notifier) == 0  # not resent
    assert len(notifier.sent) == 1


async def test_rerendered_clip_gets_a_fresh_preview(
    factory: Factory, session: Session, engine: Engine, settings: Settings, notifier: FakeNotifier, video: Path
) -> None:
    clip = pending(factory, session, video)
    fac = make_session_factory(engine)
    await send_pending_previews(fac, settings, notifier)
    # New hook: back through RENDERED with a newer rendered_at, then PENDING_REVIEW again.
    transition(session, clip, ClipState.RENDERED)
    clip.rendered_at = datetime(2026, 11, 23, 7, 0, tzinfo=UTC)
    transition(session, clip, ClipState.PENDING_REVIEW)
    session.commit()
    assert await send_pending_previews(fac, settings, notifier) == 1
    assert len(notifier.sent) == 2


async def test_non_pending_clips_get_no_preview(
    factory: Factory, session: Session, engine: Engine, settings: Settings, notifier: FakeNotifier, video: Path
) -> None:
    factory.clip(path=str(video))  # RENDERED, not PENDING_REVIEW
    session.commit()
    assert await send_pending_previews(make_session_factory(engine), settings, notifier) == 0


async def test_oversize_clip_uses_the_480p_proxy(
    factory: Factory,
    session: Session,
    engine: Engine,
    settings: Settings,
    notifier: FakeNotifier,
    video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(previews, "MAX_VIDEO_BYTES", 50)
    proxy = video.with_name("clip.preview.mp4")
    proxy.write_bytes(b"p" * 10)
    pending(factory, session, video)
    await send_pending_previews(make_session_factory(engine), settings, notifier)
    assert notifier.sent[0].path == proxy
    assert notifier.sent[0].keyboard is not None


async def test_oversize_clip_without_proxy_is_text_only_with_no_buttons(
    factory: Factory,
    session: Session,
    engine: Engine,
    settings: Settings,
    notifier: FakeNotifier,
    video: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(previews, "MAX_VIDEO_BYTES", 50)
    pending(factory, session, video)
    await send_pending_previews(make_session_factory(engine), settings, notifier)
    (sent,) = notifier.sent
    assert sent.kind == "text"
    assert sent.keyboard is None
    assert "no 480p proxy" in sent.text
    with session_scope(make_session_factory(engine)) as s:
        assert s.scalars(select(EventRow).where(EventRow.to_state == alerts.PREVIEW_TOO_BIG)).first() is not None


def test_pick_video_missing_file(tmp_path: Path) -> None:
    assert pick_video(str(tmp_path / "nope.mp4")) == (None, "clip file is missing")


async def test_failed_send_is_retried_next_tick(
    factory: Factory, session: Session, engine: Engine, settings: Settings, notifier: FakeNotifier, video: Path
) -> None:
    pending(factory, session, video)
    fac = make_session_factory(engine)
    notifier.fail = True
    assert await send_pending_previews(fac, settings, notifier) == 0
    notifier.fail = False
    assert await send_pending_previews(fac, settings, notifier) == 1


# --- alert delivery ----------------------------------------------------------------------------------------------


async def test_alerts_delivered_in_order_with_cursor(engine: Engine, notifier: FakeNotifier) -> None:
    fac = make_session_factory(engine)
    with session_scope(fac) as s:
        alerts.raise_alert(s, "download_failed", 1, "first")
        s.add(EventRow(entity="clip", entity_id="1", to_state="approved", detail="not an alert"))
        costs.pause(s, "monthly cap")
        alerts.raise_alert(s, "no_speech", 2, "second")
    assert await deliver_pending_alerts(fac, notifier) == 3
    assert notifier.texts == [
        "Alert (download_failed): first",
        "Pipeline paused: monthly cap",
        "Alert (no_speech): second",
    ]
    assert await deliver_pending_alerts(fac, notifier) == 0
    with session_scope(fac) as s:
        assert get_flag(s, ALERT_CURSOR) is not None


async def test_alert_delivery_failure_keeps_the_alert_for_retry(engine: Engine, notifier: FakeNotifier) -> None:
    fac = make_session_factory(engine)
    with session_scope(fac) as s:
        alerts.raise_alert(s, "x", 1, "a")
        alerts.raise_alert(s, "y", 2, "b")
    notifier.fail = True
    assert await deliver_pending_alerts(fac, notifier) == 0
    notifier.fail = False
    assert await deliver_pending_alerts(fac, notifier) == 2


def test_alert_urls_lose_their_query_string(session: Session) -> None:
    ev = alerts.raise_alert(session, "x", 1, "failed https://example.com/a/b?token=SECRET&x=1 ok")
    assert "SECRET" not in ev.detail
    assert "https://example.com/a/b" in ev.detail


# --- worker watchdog ---------------------------------------------------------------------------------------------


EVENING = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)  # 20:00 ET


def test_watchdog_only_inside_the_window(settings: Settings) -> None:
    assert worker_is_stale(settings, EVENING)  # no heartbeat ever
    assert not worker_is_stale(settings, datetime(2026, 11, 24, 15, 0, tzinfo=UTC))  # 10:00 ET
    assert not worker_is_stale(settings, datetime(2026, 11, 24, 2, 30, tzinfo=UTC))  # 21:30 ET


def test_watchdog_fresh_and_stale_heartbeat(settings: Settings) -> None:
    heartbeat.beat(settings.heartbeat_dir, "worker")
    # Pretend "now" is a few minutes after the beat, inside the evening window.
    import os

    path = settings.heartbeat_dir / "worker"
    base = EVENING.timestamp()
    os.utime(path, (base - 60, base - 60))
    assert not worker_is_stale(settings, EVENING)
    os.utime(path, (base - 700, base - 700))
    assert worker_is_stale(settings, EVENING)


def test_watchdog_alerts_once_per_evening(session: Session, settings: Settings) -> None:
    assert check_worker_heartbeat(session, settings, EVENING)
    assert not check_worker_heartbeat(session, settings, EVENING + timedelta(minutes=5))
    kinds = [e.to_state for e in session.scalars(select(EventRow).where(EventRow.entity == "alert"))]
    assert kinds == [alerts.WORKER_STALE]
    assert check_worker_heartbeat(session, settings, EVENING + timedelta(days=1))  # next evening alerts again


async def test_ingest_alerts_reach_the_operator(engine: Engine, notifier: FakeNotifier) -> None:
    """CR-A1 close-out: alerts written by ingest/transcribe (`ingest.alerts.alert_operator`) are delivered by the bot,
    with URL query strings already removed."""
    from clipforge.ingest import alerts as ingest_alerts

    fac = make_session_factory(engine)
    with session_scope(fac) as s:
        ingest_alerts.alert_operator(
            s, ingest_alerts.HOST_REFUSED, 7, "download refused: https://www.youtube.com/watch?v=abc&token=secret"
        )
        ingest_alerts.alert_operator(s, ingest_alerts.NO_SPEECH, 8, "source 8 has 4 words per minute")
    assert await deliver_pending_alerts(fac, notifier) == 2
    assert notifier.texts == [
        "Alert (host_refused): download refused: https://www.youtube.com/watch?…",
        "Alert (no_speech): source 8 has 4 words per minute",
    ]
