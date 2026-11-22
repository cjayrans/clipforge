"""Invariant 3: every rendered clip has the 'Ad' label from 0.0 s to >= 3.0 s (manifest, checked by QA)."""

from __future__ import annotations

import pytest

from clipforge.compliance import ComplianceError, assert_render_manifest, render_manifest_problems
from clipforge.config import AppConfig
from tests.compliance._helpers import manifest, xfail_until


def test_ad_label_first_three_seconds() -> None:
    assert_render_manifest(manifest())
    assert_render_manifest(manifest(ad_label_end_s=4.0))
    with pytest.raises(ComplianceError, match="no 'Ad' label"):
        assert_render_manifest(manifest(has_ad_label=False))
    with pytest.raises(ComplianceError, match="must start at 0.0s"):
        assert_render_manifest(manifest(ad_label_start_s=0.5))
    with pytest.raises(ComplianceError, match=">= 3.0s"):
        assert_render_manifest(manifest(ad_label_end_s=2.99))


def test_manifest_problems_lists_everything() -> None:
    problems = render_manifest_problems(
        manifest(has_ad_label=False, ad_label_end_s=1.0, audio_source_only=False, width=720, duration_s=5.0)
    )
    assert len(problems) == 5


def test_config_cannot_shorten_ad_label(app_config: AppConfig) -> None:
    assert app_config.render.ad_label_seconds >= 3.0


@xfail_until("Phase 1 C: clipforge.render.overlays.ad_label_event")
def test_overlay_ad_label_spans_first_three_seconds(app_config: AppConfig) -> None:
    from clipforge.render import overlays

    event = overlays.ad_label_event(app_config.render)  # type: ignore[attr-defined]
    assert event.start_s == 0.0
    assert event.end_s >= 3.0
    assert event.text == app_config.render.ad_label_text
