"""Tests never run with live publishing; the bot ignores every chat but the owner's."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.guards import LivePublishModeError, refuse_live_publish_mode

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("value", ["live", "LIVE", " live "])
def test_guard_refuses_live(value: str) -> None:
    with pytest.raises(LivePublishModeError):
        refuse_live_publish_mode({"PUBLISH_MODE": value})


@pytest.mark.parametrize("env", [{}, {"PUBLISH_MODE": "dry_run"}, {"PUBLISH_MODE": "private"}])
def test_guard_allows_other_modes(env: dict[str, str]) -> None:
    refuse_live_publish_mode(env)


def test_pytest_session_refuses_live_mode() -> None:
    """Run a pure test in a child pytest with live mode requested: the session must refuse to start."""
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.startswith("TELEGRAM_")}
    env["PUBLISH_MODE"] = "live"
    target = "tests/compliance/test_live_mode_guard.py::test_guard_allows_other_modes"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", target],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode != 0
    assert "refusing to run tests" in proc.stdout + proc.stderr
    assert "passed" not in proc.stdout


def test_bot_ignores_other_chats() -> None:
    from clipforge.review.bot import is_owner_chat  # type: ignore[attr-defined]

    assert is_owner_chat(chat_id=111, owner_chat_id=111)
    assert not is_owner_chat(chat_id=222, owner_chat_id=111)
    assert not is_owner_chat(chat_id=111, owner_chat_id=None)
