"""A1: campaign registry (add/upsert, list, drain/end, check) and source registration refusals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CampaignRow, EventRow, SourceRow
from clipforge.ingest import registry
from clipforge.ingest.models import campaign_from_row
from clipforge.ingest.registry import RegistryError
from clipforge.schemas import CampaignStatus
from tests.support.factories import Factory, campaign_in

REPO = Path(__file__).resolve().parents[2]
CAMPAIGN_FILES = sorted((REPO / "campaigns").glob("*.json"))


def _write(tmp_path: Path, data: dict[str, Any], name: str = "c.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _campaign_json(**overrides: Any) -> dict[str, Any]:
    data = campaign_in(**overrides).model_dump(mode="json")
    return data


def _alerts(session: Session) -> list[EventRow]:
    return list(session.scalars(select(EventRow).where(EventRow.entity == "alert")))


# --- add / upsert -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", CAMPAIGN_FILES, ids=lambda p: p.name)
def test_repo_campaign_files_register(session: Session, settings: Settings, path: Path) -> None:
    row = registry.add_campaign_from_json(session, settings, path)
    assert row.id is not None
    assert row.status is CampaignStatus.ACTIVE
    # Round-trip back to the contract model.
    c = campaign_from_row(row)
    assert c.name == row.name
    assert c.allow_stream_download is False


def test_add_is_upsert_by_slug(session: Session, settings: Settings, tmp_path: Path) -> None:
    data = _campaign_json(name="Upsert Me", cpm_usd=1.0)
    first = registry.add_campaign_from_json(session, settings, _write(tmp_path, data))
    registry.set_campaign_status(session, settings, first.id, CampaignStatus.DRAINED)
    data["cpm_usd"] = 2.5
    second = registry.add_campaign_from_json(session, settings, _write(tmp_path, data, "c2.json"))
    assert second.id == first.id
    assert second.cpm_usd == 2.5
    assert second.status is CampaignStatus.DRAINED  # update never changes status
    assert len(list(session.scalars(select(CampaignRow)))) == 1
    details = [e.detail for e in session.scalars(select(EventRow).where(EventRow.entity == "campaign"))]
    assert details[0] == "added"
    assert "updated" in details


def test_add_registers_source_urls(session: Session, settings: Settings, tmp_path: Path) -> None:
    data = _campaign_json(
        source_urls=["https://cdn.example.com/ep1.mp4", "https://www.youtube.com/watch?v=abc"],
    )
    row = registry.add_campaign_from_json(session, settings, _write(tmp_path, data))
    urls = list(session.scalars(select(SourceRow.url).where(SourceRow.campaign_id == row.id)))
    assert urls == ["https://cdn.example.com/ep1.mp4"]  # YouTube refused
    assert [a.to_state for a in _alerts(session)] == ["host_refused"]


def test_bad_json_and_schema_errors(session: Session, settings: Settings, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        registry.add_campaign_from_json(session, settings, bad)
    data = _campaign_json()
    data["cpm_usd"] = -1
    data["surprise"] = True
    with pytest.raises(RegistryError, match="CampaignIn") as exc:
        registry.add_campaign_from_json(session, settings, _write(tmp_path, data))
    assert "cpm_usd" in exc.value.message
    assert "surprise" in exc.value.message
    with pytest.raises(RegistryError, match="not found"):
        registry.add_campaign_from_json(session, settings, tmp_path / "missing.json")


def test_list_campaigns(session: Session, settings: Settings, factory: Factory, capsys: Any) -> None:
    assert registry.list_campaigns(session) == []
    a = factory.campaign(name="Alpha")
    factory.source(a)
    factory.campaign(name="Beta")
    rows = registry.list_campaigns(session, settings)
    assert [r.name for r in rows] == ["Alpha", "Beta"]
    out = capsys.readouterr().out
    assert "Alpha" in out
    assert "Beta" in out


# --- status -------------------------------------------------------------------------------------------


def test_status_transitions(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign()
    registry.set_campaign_status(session, settings, c.id, CampaignStatus.DRAINED)
    assert c.status is CampaignStatus.DRAINED
    registry.set_campaign_status(session, settings, c.id, CampaignStatus.ACTIVE)  # budget top-up
    registry.set_campaign_status(session, settings, c.id, CampaignStatus.ENDED)
    with pytest.raises(RegistryError, match="cannot become"):
        registry.set_campaign_status(session, settings, c.id, CampaignStatus.ACTIVE)
    with pytest.raises(RegistryError, match="cannot become"):
        registry.set_campaign_status(session, settings, c.id, CampaignStatus.DRAINED)
    # Same status is a no-op, not an error.
    registry.set_campaign_status(session, settings, c.id, CampaignStatus.ENDED)
    events = list(session.scalars(select(EventRow).where(EventRow.entity == "campaign")))
    assert [(e.from_state, e.to_state) for e in events] == [
        ("active", "drained"),
        ("drained", "active"),
        ("active", "ended"),
    ]
    assert all(e.actor == "operator" for e in events)


def test_unknown_campaign(session: Session, settings: Settings) -> None:
    with pytest.raises(RegistryError, match="no campaign with id 99"):
        registry.set_campaign_status(session, settings, 99, CampaignStatus.DRAINED)
    with pytest.raises(RegistryError):
        registry.check_campaign(session, settings, 99)
    with pytest.raises(RegistryError):
        registry.add_source(session, settings, 99, "https://cdn.example.com/a.mp4")


# --- campaign filter ----------------------------------------------------------------------------------

GOOD: dict[str, Any] = {
    "approval_rate_pct": 60,
    "budget_remaining_usd": 4000,
    "per_clip_cap_usd": 250,
    "min_payout_usd": 4,
    "cpm_usd": 1.2,
    "participants": 4,
    "footage_mode": "supplied",
}


def _checks(settings: Settings, **overrides: Any) -> dict[str, bool]:
    c = campaign_in(**{**GOOD, **overrides})
    return {ch.name: ch.passed for ch in registry.campaign_checks(c, settings)}


def test_good_campaign_passes_every_check(settings: Settings) -> None:
    checks = _checks(settings)
    assert all(checks.values()), checks
    assert set(checks) >= {
        "approval_rate",
        "budget_remaining",
        "budget_to_cap_ratio",
        "views_to_min_payout_tiktok",
        "views_to_min_payout_instagram",
        "participants",
        "footage_mode",
        "accepts_tiktok_instagram",
        "not_political",
    }


@pytest.mark.parametrize(
    ("overrides", "failing"),
    [
        ({"approval_rate_pct": 30}, "approval_rate"),
        ({"budget_remaining_usd": 900, "per_clip_cap_usd": 100}, "budget_remaining"),
        ({"budget_remaining_usd": None}, "budget_remaining"),
        ({"budget_remaining_usd": 1200, "per_clip_cap_usd": 300}, "budget_to_cap_ratio"),
        ({"min_payout_by_platform": {"tiktok": 20}, "cpm_usd": 1.0}, "views_to_min_payout_tiktok"),  # 20,000 views
        ({"min_payout_by_platform": {"instagram": 50}}, "views_to_min_payout_instagram"),
        ({"cpm_by_platform": {"tiktok": 0}}, "views_to_min_payout_tiktok"),
        ({"participants": 150}, "participants"),
        ({"footage_mode": "own_gameplay"}, "footage_mode"),
        ({"allowed_platforms": ["tiktok", "youtube"]}, "accepts_tiktok_instagram"),
        ({"is_political": True}, "not_political"),
    ],
)
def test_each_rule_can_fail(settings: Settings, overrides: dict[str, Any], failing: str) -> None:
    checks = _checks(settings, **overrides)
    assert checks[failing] is False
    assert [k for k, v in checks.items() if not v] == [failing]


def test_unshown_values_skip_their_rule(settings: Settings) -> None:
    checks = _checks(settings, approval_rate_pct=None, participants=None, per_clip_cap_usd=None)
    assert all(checks.values())


def test_views_to_min_payout() -> None:
    assert registry.views_to_min_payout(4.0, 1.2) == pytest.approx(3333.33, rel=1e-3)
    assert registry.views_to_min_payout(0.0, 0.0) == 0.0
    assert registry.views_to_min_payout(10.0, 0.0) is None


def test_check_campaign_on_repo_files(session: Session, settings: Settings) -> None:
    for path in CAMPAIGN_FILES:
        row = registry.add_campaign_from_json(session, settings, path)
        checks = registry.check_campaign(session, settings, row.id)
        by_name = {c.name: c.passed for c in checks}
        assert by_name["footage_mode"] is True
        assert by_name["accepts_tiktok_instagram"] is True


# --- sources: registration and refusals ---------------------------------------------------------------


def test_add_source_registers_normalized_and_is_idempotent(
    session: Session, settings: Settings, factory: Factory
) -> None:
    c = factory.campaign()
    rows = registry.add_source(session, settings, c.id, "  https://CDN.example.com/ep1.mp4#t=3 ")
    assert [r.url for r in rows] == ["https://cdn.example.com/ep1.mp4"]
    again = registry.add_source(session, settings, c.id, "https://cdn.example.com/ep1.mp4")
    assert [r.id for r in again] == [rows[0].id]
    assert session.scalars(select(SourceRow)).all() == rows
    ev = session.scalars(select(EventRow).where(EventRow.entity == "source")).one()
    assert ev.to_state == "registered"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://www.youtube.com/watch?v=abc", "YouTube"),
        ("https://youtu.be/abc", "YouTube"),
        ("https://kick.com/someone/videos/1", "Kick"),
        ("https://www.instagram.com/reel/abc/", "host not allowed"),
        ("https://contentrewards.com/files/x.mp4", "host not allowed"),
        ("https://cdn.example.com/watch?v=1", "host not allowed"),
        ("https://www.twitch.tv/videos/123456", "allow_stream_download"),
        ("https://www.dropbox.com/scl/fo/abc/def?rlkey=x&dl=0", "Dropbox folder"),
        ("https://www.dropbox.com/sh/abc/def?dl=0", "Dropbox folder"),
        ("file://inbox/x/ep1.mp4", "inbox scan"),
        ("file:///etc/passwd", "host not allowed"),
    ],
)
def test_add_source_refuses_with_alert(
    session: Session, settings: Settings, factory: Factory, capsys: Any, url: str, reason: str
) -> None:
    c = factory.campaign()
    assert registry.add_source(session, settings, c.id, url) == []
    assert session.scalars(select(SourceRow)).all() == []
    alert = _alerts(session)
    assert len(alert) == 1
    assert alert[0].to_state == "host_refused"
    assert reason in alert[0].detail
    assert reason in capsys.readouterr().out


def test_twitch_registers_with_permission(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(allow_stream_download=True)
    rows = registry.add_source(session, settings, c.id, "https://www.twitch.tv/videos/123456")
    assert len(rows) == 1
    assert _alerts(session) == []


def test_youtube_refused_even_with_stream_permission(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign(allow_stream_download=True)
    assert registry.add_source(session, settings, c.id, "https://www.youtube.com/watch?v=abc") == []


@pytest.mark.parametrize("status", ["drained", "ended"])
def test_inactive_campaign_refuses_sources(session: Session, settings: Settings, factory: Factory, status: str) -> None:
    c = factory.campaign(status=status)
    assert registry.add_source(session, settings, c.id, "https://cdn.example.com/ep1.mp4") == []
    assert _alerts(session)[0].to_state == "campaign_inactive"


def test_dropbox_file_link_registers(session: Session, settings: Settings, factory: Factory) -> None:
    c = factory.campaign()
    rows = registry.add_source(session, settings, c.id, "https://www.dropbox.com/scl/fi/abc/ep1.mp4?rlkey=x&dl=0")
    assert len(rows) == 1
