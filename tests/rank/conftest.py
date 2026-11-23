from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from clipforge.config import AppConfig
from clipforge.rank.claude_client import ClaudeClient
from clipforge.schemas import Transcript
from tests.rank.anthropic_mock import FakeAnthropic

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def fake() -> FakeAnthropic:
    return FakeAnthropic()


@pytest.fixture
def client(fake: FakeAnthropic, session: Session, app_config: AppConfig) -> ClaudeClient:
    # Test key only; requests go to the mock transport, never the network.
    return ClaudeClient(
        "sk-test-not-a-real-key", session=session, app=app_config, http_client=fake.http_client(), backoff_s=0
    )


@pytest.fixture
def talk() -> Transcript:
    return Transcript.model_validate(json.loads((FIXTURES / "transcript_talk.json").read_text()))
