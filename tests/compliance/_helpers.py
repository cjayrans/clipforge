"""Helpers shared by the compliance tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from clipforge.schemas import Campaign, CampaignStatus, RenderManifest
from tests.support.factories import campaign_in


def campaign(status: CampaignStatus = CampaignStatus.ACTIVE, **overrides: Any) -> Campaign:
    base = campaign_in(**overrides).model_dump()
    return Campaign(id=1, status=status, created_at=datetime(2026, 11, 22, tzinfo=UTC), **base)


def manifest(**overrides: Any) -> RenderManifest:
    data: dict[str, Any] = {
        "clip_path": "/tmp/clip.mp4",
        "candidate_id": 1,
        "account_set": "gaming-1",
        "style_preset": "bold-yellow",
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "duration_s": 35.0,
        "has_ad_label": True,
        "ad_label_start_s": 0.0,
        "ad_label_end_s": 3.0,
        "hook_text": "hook",
        "hook_end_s": 2.5,
        "audio_source_only": True,
        "sha256": "0" * 64,
    }
    data.update(overrides)
    return RenderManifest(**data)


def xfail_until(phase: str) -> Any:
    """Strict xfail naming the phase/module that makes the test pass. Remove the marker in P2.5 (or in the Phase 1
    branch whose module makes it pass)."""
    import pytest

    return pytest.mark.xfail(strict=True, reason=f"needs {phase}; remove this marker when it passes (P2.5)")
