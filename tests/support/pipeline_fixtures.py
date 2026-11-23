"""Pipeline test fixtures (registered in tests/conftest.py): fake API keys, run_once's database, wired fakes."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from clipforge.config import Settings, load_settings
from clipforge.db import CampaignRow, make_engine, make_session_factory, session_scope
from clipforge.db.migrate import upgrade
from clipforge.ingest.registry import add_source, upsert_campaign
from clipforge.pipeline import PipelineDeps
from clipforge.transcribe import groq_client
from tests.rank.anthropic_mock import FakeAnthropic
from tests.support.factories import campaign_in
from tests.support.pipeline_fakes import (
    GROQ_URL,
    SOURCE_URL,
    FakeRender,
    fake_anthropic,
    groq_verbose_json,
    media_runner,
)

OWNER = 111
NIGHT = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)  # 01:00 America/New_York, Mon 23 Nov 2026


@pytest.fixture
def pipe_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Fake keys only: every request goes to a mock. PUBLISH_MODE is never set (dry_run default).
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key-not-a-real-one")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token-for-tests")
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", str(OWNER))
    monkeypatch.setattr(groq_client, "MAX_RETRY_AFTER_S", 0.0)
    return load_settings()


@pytest.fixture
def pipe_engine(pipe_settings: Settings) -> Iterator[Engine]:
    upgrade(pipe_settings.database_url)
    eng = make_engine(pipe_settings.database_url)
    yield eng
    eng.dispose()


@pytest.fixture
def db(pipe_engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(pipe_engine)


@pytest.fixture
def anthropic() -> FakeAnthropic:
    return fake_anthropic()


@pytest.fixture
def render() -> FakeRender:
    return FakeRender()


@dataclass
class Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(NIGHT)


@pytest.fixture
def deps(anthropic: FakeAnthropic, render: FakeRender, clock: Clock) -> PipelineDeps:
    return PipelineDeps(
        local_transcriber=None,
        media_runner=media_runner(),
        claude_http_client=anthropic.http_client(),
        render_kwargs=render.kwargs(),
        qa_probe=render.probe,
        clock=clock,
    )


@pytest.fixture
def mocked_http() -> Iterator[respx.MockRouter]:
    """Groq answers with the talk fixture; the direct footage link serves bytes; Upload-Post must never be called."""
    with respx.mock(assert_all_called=False) as router:
        router.post(GROQ_URL).mock(return_value=httpx.Response(200, json=groq_verbose_json()))
        router.get(SOURCE_URL).mock(
            return_value=httpx.Response(
                200, content=b"\x00\x00\x00\x18ftypmp42 fake", headers={"content-type": "video/mp4"}
            )
        )
        router.route(host="api.upload-post.com").mock(return_value=httpx.Response(500))
        yield router


def add_campaign(
    db: sessionmaker[Session], settings: Settings, *, url: str = SOURCE_URL, **overrides: object
) -> tuple[int, int]:
    """Register a campaign and one source through the real registry. Returns (campaign id, source id)."""
    data: dict[str, object] = {
        "name": "Dylan Page GTA VI Marathon",
        "allowed_platforms": ["tiktok", "instagram"],
        "required_tags": ["#GTA6"],
    }
    data.update(overrides)
    with session_scope(db) as s:
        row, _ = upsert_campaign(s, campaign_in(**data))
        sources = add_source(s, settings, row.id, url)
        return row.id, sources[0].id


def campaign_row(db: sessionmaker[Session], campaign_id: int) -> CampaignRow:
    with session_scope(db) as s:
        row = s.get(CampaignRow, campaign_id)
        assert row is not None
        return row


def write_bytes(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path
