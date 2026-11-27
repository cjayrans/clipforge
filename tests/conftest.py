"""Test configuration.

- Refuses to run if PUBLISH_MODE is live (docs/COMPLIANCE.md).
- Every test gets a clean environment: no secrets, config from config/clipforge.example.yaml (never the operator's
  local config/clipforge.yaml), and data/inbox dirs under tmp_path.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from clipforge.config import SECRET_ENV_VARS, AppConfig, Settings, load_app_config, load_settings
from clipforge.db import Base, make_engine, make_session_factory
from tests.support.factories import Factory
from tests.support.guards import LivePublishModeError, refuse_live_publish_mode

pytest_plugins = ["tests.support.pipeline_fixtures"]

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "config" / "clipforge.example.yaml"
FIXTURES = REPO_ROOT / "tests" / "fixtures"


def pytest_configure(config: pytest.Config) -> None:
    try:
        refuse_live_publish_mode(os.environ)
    except LivePublishModeError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (*SECRET_ENV_VARS, "PUBLISH_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLIPFORGE_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("CLIPFORGE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLIPFORGE_INBOX_DIR", str(tmp_path / "inbox"))
    # The disk guard (ingest/disk.py) sees plenty of free space unless a test sets its own.
    monkeypatch.setattr("clipforge.ingest.disk.free_bytes", lambda _directory: 10**15)


@pytest.fixture
def app_config() -> AppConfig:
    return load_app_config(EXAMPLE_CONFIG)


@pytest.fixture
def settings() -> Settings:
    return load_settings()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    s = factory()
    try:
        yield s
    finally:
        s.rollback()
        s.close()


@pytest.fixture
def factory(session: Session) -> Factory:
    return Factory(session)
