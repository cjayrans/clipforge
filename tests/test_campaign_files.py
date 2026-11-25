"""P0.12: every file in campaigns/ validates against schemas.CampaignIn and passes basic sanity rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipforge.compliance import allowed_platforms, assert_registered_source, classify_source_host, contains_url
from clipforge.schemas import Campaign, CampaignIn, CampaignStatus, Platform, SourceHost

CAMPAIGNS = sorted((Path(__file__).resolve().parent.parent / "campaigns").glob("*.json"))


def test_campaign_files_exist() -> None:
    assert len(CAMPAIGNS) >= 2


@pytest.mark.parametrize("path", CAMPAIGNS, ids=lambda p: p.name)
def test_campaign_file_validates(path: Path) -> None:
    c = CampaignIn.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert c.footage_mode != "own_gameplay"  # screening rule 1 (docs/CAMPAIGNS.md)
    assert {Platform.TIKTOK, Platform.INSTAGRAM} <= set(allowed_platforms(c))  # screening rule 3
    assert c.disclosure_text.lower().startswith("#ad")
    for tag in c.required_tags:
        # A hashtag, or a link the brief requires (e.g. the streamer's channel); a link can never go on X (inv. 9).
        assert tag.startswith("#") or (contains_url(tag) and Platform.X not in c.allowed_platforms), tag
    if c.min_views_per_post and c.min_payout_usd:
        # min_views_per_post is derived from min payout / CPM (rounded up).
        assert c.min_views_per_post >= c.min_payout_usd / c.cpm_usd * 1000 - 1


@pytest.mark.parametrize("path", CAMPAIGNS, ids=lambda p: p.name)
def test_campaign_source_urls_are_downloadable_hosts(path: Path) -> None:
    c = CampaignIn.model_validate(json.loads(path.read_text(encoding="utf-8")))
    full = Campaign(id=1, status=CampaignStatus.ACTIVE, created_at="2026-11-22T00:00:00Z", **c.model_dump())
    for url in c.source_urls:
        host = classify_source_host(str(url))
        assert host not in (SourceHost.YOUTUBE, SourceHost.KICK, SourceHost.OTHER), url
        assert assert_registered_source(str(url), full, [str(url)]) is host
