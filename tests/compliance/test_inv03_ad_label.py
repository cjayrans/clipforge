"""Invariant 3: every rendered clip has the 'Ad' label from 0.0 s to >= 3.0 s (manifest, checked by QA), and a
campaign overlay never covers it."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.compliance import ComplianceError, assert_render_manifest, render_manifest_problems
from clipforge.config import AppConfig
from clipforge.schemas import CampaignOverlay, OverlayManifest, Rect
from tests.compliance._helpers import manifest
from tests.render._media import needs_ffmpeg


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


def test_overlay_ad_label_spans_first_three_seconds(app_config: AppConfig) -> None:
    from clipforge.render import overlays

    event = overlays.ad_label_event(app_config.render)  # type: ignore[attr-defined]
    assert event.start_s == 0.0
    assert event.end_s >= 3.0
    assert event.text == app_config.render.ad_label_text


# --- with a campaign overlay (generic; any campaign may supply one) -------------------------------------


def _overlay(**kw: object) -> OverlayManifest:
    data: dict[str, object] = {
        "image_sha256": "0" * 64,
        "rect": Rect(x0=270, y0=487, x1=810, y1=623),
        "opaque_rect": Rect(x0=297, y0=507, x1=783, y1=603),
        "start_s": 0.0,
        "end_s": 35.0,
        "full_duration": True,
    }
    data.update(kw)
    return OverlayManifest.model_validate(data)


AD_RECT = Rect(x0=38, y0=110, x1=130, y1=190)


def test_ad_label_never_covered_by_campaign_overlay() -> None:
    assert_render_manifest(manifest(ad_label_rect=AD_RECT, overlay=_overlay()))
    covering = _overlay(opaque_rect=Rect(x0=0, y0=100, x1=400, y1=220))
    with pytest.raises(ComplianceError, match="covers the 'Ad' label"):
        assert_render_manifest(manifest(ad_label_rect=AD_RECT, overlay=covering))
    with pytest.raises(ComplianceError, match="no recorded 'Ad' label position"):
        assert_render_manifest(manifest(overlay=_overlay()))


def test_full_duration_overlay_covers_the_whole_clip() -> None:
    with pytest.raises(ComplianceError, match="must cover the whole clip"):
        assert_render_manifest(manifest(ad_label_rect=AD_RECT, overlay=_overlay(end_s=20.0)))
    with pytest.raises(ComplianceError, match="must cover the whole clip"):
        assert_render_manifest(manifest(ad_label_rect=AD_RECT, overlay=_overlay(start_s=2.5)))
    # An overlay the campaign shows only after the hook is not held to the whole clip.
    assert_render_manifest(manifest(ad_label_rect=AD_RECT, overlay=_overlay(start_s=2.5, full_duration=False)))


def test_overlay_never_relaxes_the_ad_label_timing() -> None:
    with pytest.raises(ComplianceError, match=">= 3.0s"):
        assert_render_manifest(manifest(ad_label_end_s=2.0, ad_label_rect=AD_RECT, overlay=_overlay()))


def test_render_keeps_ad_label_clear_of_any_overlay_placement(app_config: AppConfig) -> None:
    """Whatever the overlay covers, the label goes to a clear spot or the render is refused; never covered."""
    from clipforge.render import campaign_overlay as co

    for opaque in (
        Rect(x0=0, y0=0, x1=10, y1=10),
        Rect(x0=0, y0=100, x1=400, y1=220),
        Rect(x0=600, y0=90, x1=1080, y1=200),
    ):
        p = co.OverlayPlacement(path=Path("o.png"), rect=opaque, opaque_rect=opaque, width=2, height=2, sha256="0")
        ad = co.place_ad_label(app_config.render.ad_label_text, p)
        assert not ad.rect.intersects(opaque)


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake campaign's overlay (magenta box with transparent margins) under tmp/campaigns/assets/."""
    import clipforge.render.clip as clip_mod
    from tests.support.overlay_png import write_overlay_png

    write_overlay_png(tmp_path)
    monkeypatch.setattr(clip_mod, "project_root", lambda: tmp_path)
    return tmp_path


@needs_ffmpeg
def test_real_render_overlay_whole_clip_and_ad_label_visible(
    tmp_path: Path, app_config: AppConfig, overlay_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real pixels: the overlay's magenta box is on screen at the start and at the end of the clip, and the "Ad"
    label box still changes the frame at 0.5 s exactly as without an overlay."""
    from clipforge.render.clip import render_candidate
    from tests.render.test_clip import _gray_region, _mean_abs_diff, request
    from tests.support.overlay_png import FAKE_OVERLAY_REL

    fake_overlay = CampaignOverlay.model_validate(
        {"path": FAKE_OVERLAY_REL, "position": {"x_pct": 50, "y_pct": 31.5}, "scale": 0.5}
    )

    req = request(tmp_path, start_s=40.0, end_s=52.0, content_kind="game", hook="", words=[])
    plain = render_candidate(req, app_config)
    with_overlay = render_candidate(
        request(tmp_path, start_s=40.0, end_s=52.0, content_kind="game", hook="", words=[], overlay=fake_overlay,
                out_dir=tmp_path / "ov"),
        app_config,
    )  # fmt: skip
    assert with_overlay.overlay is not None
    o = with_overlay.overlay.opaque_rect
    box = (o.x0 + 20, o.y0 + 10, 100, 40)  # inside the magenta box
    ad_box = (40, 95, 160, 70)
    diffs = {
        t: _mean_abs_diff(_gray_region(with_overlay.clip_path, t, *box), _gray_region(plain.clip_path, t, *box))
        for t in (0.5, 6.0, 11.5)
    }
    ad = _mean_abs_diff(_gray_region(with_overlay.clip_path, 0.5, *ad_box), _gray_region(plain.clip_path, 0.5, *ad_box))
    with capsys.disabled():
        print(f"\nOVERLAY mean grey diff vs no-overlay render: {diffs}; 'Ad' label box diff: {ad}")
    assert all(d > 15 for d in diffs.values()), diffs  # overlay visible for the whole clip
    assert ad < 2, ad  # the "Ad" label renders exactly as without the overlay
