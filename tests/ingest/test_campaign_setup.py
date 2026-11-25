"""Registering a campaign with the generic optional fields (fake campaign): overlay checked before anything is
saved, URL-like required caption text refused on X, stream channel/length limits stored."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CampaignRow
from clipforge.ingest import registry
from clipforge.ingest.models import campaign_from_row
from clipforge.ingest.registry import RegistryError
from tests.support.factories import campaign_in
from tests.support.overlay_png import FAKE_OVERLAY_REL, write_overlay_png


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(registry, "project_root", lambda: tmp_path)
    write_overlay_png(tmp_path)
    return tmp_path


def _json(tmp_path: Path, **kw: Any) -> Path:
    data = campaign_in(name="Fake Streamer Clips", **kw).model_dump(mode="json")
    path = tmp_path / "fake.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


OVERLAY = {"path": FAKE_OVERLAY_REL, "position": {"x_pct": 50, "y_pct": 31.5}, "scale": 0.5, "full_duration": True}


def test_campaign_with_overlay_registers(session: Session, settings: Settings, root: Path) -> None:
    path = _json(
        root,
        overlay=OVERLAY,
        required_caption_tags=["twitch.tv/fakestreamer"],
        copy_style_notes="meme tone",
        allow_stream_download=True,
        stream_channels=["FakeStreamer"],
        max_source_minutes=60,
        footage_mode="streamer_stream",
    )
    row = registry.add_campaign_from_json(session, settings, path)
    c = campaign_from_row(row)
    assert c.overlay is not None
    assert c.overlay.path == FAKE_OVERLAY_REL
    assert c.overlay.scale == 0.5
    assert c.required_tags == ["twitch.tv/fakestreamer"]
    assert c.copy_style_notes == "meme tone"
    assert c.stream_channels == ["fakestreamer"]
    assert c.max_source_minutes == 60


@pytest.mark.parametrize(
    ("position", "what"),
    [
        ({"x_pct": 50, "y_pct": 66}, "captions"),
        ({"x_pct": 50, "y_pct": 18}, "hook"),
        ({"x_pct": 50, "y_pct": 92}, "bottom"),
    ],
)
def test_bad_overlay_placement_refused_and_not_saved(
    session: Session, settings: Settings, root: Path, position: dict[str, float], what: str
) -> None:
    path = _json(root, overlay={**OVERLAY, "position": position})
    with pytest.raises(RegistryError, match=what):
        registry.add_campaign_from_json(session, settings, path)
    assert session.scalars(select(CampaignRow)).all() == []


def test_missing_overlay_file_refused(session: Session, settings: Settings, root: Path) -> None:
    (root / FAKE_OVERLAY_REL).unlink()
    with pytest.raises(RegistryError, match="not found"):
        registry.add_campaign_from_json(session, settings, _json(root, overlay=OVERLAY))


def test_campaign_without_new_fields_registers_as_before(session: Session, settings: Settings, tmp_path: Path) -> None:
    row = registry.add_campaign_from_json(session, settings, _json(tmp_path))
    c = campaign_from_row(row)
    assert c.overlay is None
    assert c.copy_style_notes == ""
    assert c.stream_channels == []
    assert c.max_source_minutes is None


def test_null_unknown_minimums_are_stored_as_zero() -> None:
    c = campaign_in(min_payout_usd=None, min_views_per_post=None)
    assert (c.min_payout_usd, c.min_views_per_post) == (0.0, 0)


def test_moment_profile_is_checked_and_stored(session: Session, settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="moment_profile: moment profile not found"):
        registry.add_campaign_from_json(session, settings, _json(tmp_path, moment_profile="no-such-profile"))
    assert session.scalars(select(CampaignRow)).first() is None
    row = registry.add_campaign_from_json(session, settings, _json(tmp_path, moment_profile="gta6-streamer"))
    assert row.moment_profile == "gta6-streamer"
    assert campaign_from_row(row).moment_profile == "gta6-streamer"
