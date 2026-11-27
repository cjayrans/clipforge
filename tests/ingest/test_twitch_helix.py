"""moments-v2: recent VODs of a campaign's stream channels are PROPOSED (never registered automatically); the
operator's Add tap registers one through registry.add_source. Helix HTTP is mocked with respx."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import EventRow, SourceRow, VodProposalRow
from clipforge.ingest.twitch_helix import (
    API_BASE,
    MAX_PROPOSALS_PER_NIGHT,
    TOKEN_URL,
    HelixClient,
    HelixError,
    decide,
    discover_vods,
    parse_duration,
)
from clipforge.review import keyboards
from clipforge.review.bot import BotDeps, on_callback
from clipforge.review.previews import send_pending_vod_proposals
from tests.review.fakes import FakeNotifier, make_context, make_update
from tests.support.factories import Factory

NOW = datetime(2026, 11, 26, 5, 30, tzinfo=UTC)


def vod(vid: str, created: str, *, login: str = "fakestreamer", stream_id: str | None = None) -> dict[str, Any]:
    return {
        "id": vid, "stream_id": stream_id, "user_id": "42", "user_login": login, "user_name": login,
        "title": f"GTA day {vid}", "description": "", "created_at": created, "published_at": created,
        "url": f"https://www.twitch.tv/videos/{vid}", "thumbnail_url": "", "viewable": "public", "view_count": 1,
        "language": "en", "type": "archive", "duration": "5h3m2s", "muted_segments": None,
    }  # fmt: skip


@pytest.fixture
def helix() -> Any:
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"access_token": "tok", "expires_in": 5011271, "token_type": "bearer"}
            )
        )
        router.get(f"{API_BASE}/users").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "42", "login": "fakestreamer"}]})
        )
        router.get(f"{API_BASE}/streams").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "live-1", "type": "live"}], "pagination": {}})
        )
        router.get(f"{API_BASE}/videos").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        vod("900", "2026-11-26T01:00:00Z", stream_id="live-1"),  # still live: skipped
                        vod("899", "2026-11-25T18:00:00Z"),
                        vod("898", "2026-11-24T18:00:00Z"),
                        vod("897", "2026-11-23T18:00:00Z"),  # third: over the nightly limit
                        vod("880", "2026-11-10T18:00:00Z"),  # older than recent_vod_days
                    ],
                    "pagination": {},
                },
            )
        )
        yield router


def campaign(factory: Factory, **kw: Any):
    data = {
        "allow_stream_download": True,
        "stream_channels": ["fakestreamer"],
        "auto_propose_vods": True,
        "recent_vod_days": 7,
        "content_kind": "game",
    }
    data.update(kw)
    return factory.campaign(**data)


def test_parse_duration() -> None:
    assert parse_duration("1h0m5s") == 3605
    assert parse_duration("3m21s") == 201
    assert parse_duration("") is None
    assert parse_duration("bogus") is None


def test_campaign_needs_permission_channels_and_window() -> None:
    from tests.support.factories import campaign_in

    with pytest.raises(ValueError, match="auto_propose_vods needs"):
        campaign_in(auto_propose_vods=True, allow_stream_download=False, stream_channels=["x"], recent_vod_days=7)


def test_discovery_proposes_recent_vods_and_registers_nothing(
    helix: Any, session: Session, settings: Settings, factory: Factory
) -> None:
    camp = campaign(factory)
    factory.source(camp, url="https://www.twitch.tv/videos/899")  # already registered: skipped
    made = discover_vods(session, settings, client=HelixClient("cid", "secret"), now=NOW)
    rows = session.scalars(select(VodProposalRow).order_by(VodProposalRow.id)).all()
    assert made == len(rows) == MAX_PROPOSALS_PER_NIGHT
    assert [r.video_id for r in rows] == ["898", "897"]  # newest first; live, registered and old ones skipped
    assert all(r.decision == "pending" for r in rows)
    assert rows[0].duration_s == 5 * 3600 + 3 * 60 + 2
    assert session.scalars(select(SourceRow)).all()[0].url.endswith("/899")  # no new sources
    assert len(session.scalars(select(SourceRow)).all()) == 1
    videos = [c.request for c in helix.calls if c.request.url.path.endswith("/videos")]
    assert videos[0].headers["Authorization"] == "Bearer tok"
    assert videos[0].headers["Client-Id"] == "cid"
    assert videos[0].url.params["type"] == "archive"
    # a second night does not propose the same VODs again
    assert discover_vods(session, settings, client=HelixClient("cid", "secret"), now=NOW) == 0


def test_campaigns_without_permission_are_not_looked_up(
    helix: Any, session: Session, settings: Settings, factory: Factory
) -> None:
    factory.campaign(allow_stream_download=True, stream_channels=["fakestreamer"])  # no auto_propose_vods
    assert discover_vods(session, settings, client=HelixClient("cid", "secret"), now=NOW) == 0
    assert not [c for c in helix.calls if "/helix/" in str(c.request.url)]


def test_api_failure_alerts_and_moves_on(session: Session, settings: Settings, factory: Factory) -> None:
    campaign(factory)
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        assert discover_vods(session, settings, client=HelixClient("cid", "secret", backoff_s=0), now=NOW) == 0
    alert = session.scalars(select(EventRow).where(EventRow.entity == "alert")).one()
    assert alert.to_state == "vod_discovery_failed"
    assert "secret" not in alert.detail


def test_missing_credentials() -> None:
    with pytest.raises(HelixError):
        HelixClient("", "")


def test_add_registers_through_add_source_and_dismiss_forgets(
    helix: Any, session: Session, settings: Settings, factory: Factory
) -> None:
    camp = campaign(factory)
    discover_vods(session, settings, client=HelixClient("cid", "secret"), now=NOW)
    first, second = session.scalars(select(VodProposalRow).order_by(VodProposalRow.id)).all()
    text = decide(session, settings, first.id, add=True, now=NOW)
    assert "Registered" in text
    src = session.get(SourceRow, first.source_id)
    assert src is not None
    assert src.campaign_id == camp.id
    assert src.url == first.url
    assert first.decision == "added"
    assert "already added" in decide(session, settings, first.id, add=True, now=NOW)
    assert "Dismissed" in decide(session, settings, second.id, add=False, now=NOW)
    assert second.decision == "dismissed"
    assert second.source_id is None
    events = session.scalars(select(EventRow).where(EventRow.entity == "vod_proposal")).all()
    assert {e.actor for e in events if e.to_state in ("added", "dismissed")} == {"operator"}


def test_add_is_refused_when_the_campaign_lost_stream_permission(
    helix: Any, session: Session, settings: Settings, factory: Factory
) -> None:
    camp = campaign(factory)
    discover_vods(session, settings, client=HelixClient("cid", "secret"), now=NOW)
    row = session.scalars(select(VodProposalRow)).first()
    camp.allow_stream_download = False
    assert "refused" in decide(session, settings, row.id, add=True, now=NOW)
    assert row.decision == "pending"
    assert session.scalars(select(SourceRow)).all() == []


async def test_bot_sends_proposals_once_and_the_add_button_registers(
    helix: Any, session: Session, settings: Settings, factory: Factory, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge.config import load_settings
    from clipforge.db import make_session_factory

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token-for-tests")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "111")
    bot_settings = load_settings()
    campaign(factory)
    discover_vods(session, settings, client=HelixClient("cid", "secret"), now=NOW)
    session.commit()
    notifier = FakeNotifier()
    db = make_session_factory(engine)
    assert await send_pending_vod_proposals(db, notifier) == 2
    assert await send_pending_vod_proposals(db, notifier) == 0  # once only
    assert "Add it as a source?" in notifier.texts[0]
    markup = notifier.sent[0].keyboard
    data = markup.inline_keyboard[0][0].callback_data
    assert data.startswith(keyboards.VOD_ADD + ":")
    deps = BotDeps(settings=bot_settings, factory=db, notifier=notifier, clock=lambda: NOW)
    update = make_update(111, data, message_id=7)
    await on_callback(update, make_context(deps))
    update.callback_query.answer.assert_awaited_once_with("Added")
    session.expire_all()
    assert len(session.scalars(select(SourceRow)).all()) == 1
