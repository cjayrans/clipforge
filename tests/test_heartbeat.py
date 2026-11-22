"""Heartbeat files."""

from __future__ import annotations

import os
import time
from pathlib import Path

from clipforge import heartbeat


def test_beat_and_age(tmp_path: Path) -> None:
    assert heartbeat.age_s(tmp_path, "worker") is None
    assert not heartbeat.is_healthy(tmp_path, "worker")
    path = heartbeat.beat(tmp_path / "hb", "worker")
    assert path.exists()
    age = heartbeat.age_s(tmp_path / "hb", "worker")
    assert age is not None
    assert age < 5
    assert heartbeat.is_healthy(tmp_path / "hb", "worker")


def test_stale(tmp_path: Path) -> None:
    path = heartbeat.beat(tmp_path, "bot")
    old = time.time() - 1000
    os.utime(path, (old, old))
    assert not heartbeat.is_healthy(tmp_path, "bot")
    age = heartbeat.age_s(tmp_path, "bot")
    assert age is not None
    assert age > heartbeat.ALERT_AFTER_S
