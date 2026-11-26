"""Invariant 3: every rendered clip has the 'Ad' label from 0.0 s to >= 3.0 s (manifest, checked by QA), and a
campaign overlay never covers it."""

from __future__ import annotations

from pathlib import Path

import pytest

from clipforge.compliance import ComplianceError, assert_render_manifest, render_manifest_problems
from clipforge.config import AppConfig
from clipforge.schemas import CampaignOverlay, OverlayManifest, Rect, RenderManifest
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

    styles = app_config.styles.values()

    def at(opaque: Rect) -> co.OverlayPlacement:
        return co.OverlayPlacement(path=Path("o.png"), rect=opaque, opaque_rect=opaque, width=2, height=2, sha256="0")

    for opaque, expected_y in (
        (Rect(x0=0, y0=0, x1=10, y1=10), 548),
        (Rect(x0=0, y0=520, x1=400, y1=640), 660),  # covers the default spot: label moves under it
        (Rect(x0=600, y0=90, x1=1080, y1=200), 548),
    ):
        ad = co.place_ad_label(app_config.render.ad_label_text, at(opaque), styles)
        assert ad.y == expected_y
        assert not ad.rect.intersects(opaque)
        assert co.ad_label_problems(ad.rect, styles, at(opaque)) == []
    # No clear spot left: refused, never drawn underneath.
    with pytest.raises(co.OverlayPlacementError, match="no clear place"):
        co.place_ad_label(app_config.render.ad_label_text, at(Rect(x0=0, y0=500, x1=400, y1=1200)), styles)


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
    r = with_overlay.ad_label_rect
    assert r is not None
    ad_box = (r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0)
    diffs = {
        t: _mean_abs_diff(_gray_region(with_overlay.clip_path, t, *box), _gray_region(plain.clip_path, t, *box))
        for t in (0.5, 6.0, 11.5)
    }
    ad = _mean_abs_diff(_gray_region(with_overlay.clip_path, 0.5, *ad_box), _gray_region(plain.clip_path, 0.5, *ad_box))
    with capsys.disabled():
        print(f"\nOVERLAY mean grey diff vs no-overlay render: {diffs}; 'Ad' label box diff: {ad}")
    assert all(d > 15 for d in diffs.values()), diffs  # overlay visible for the whole clip
    assert ad < 2, ad  # the "Ad" label renders exactly as without the overlay


# --- label layout 2 (2026-11-25): below the platform UI, on a semi-opaque dark box ------------------------------

LAYOUT2_RECT = Rect(x0=36, y0=536, x1=132, y1=620)


def test_layout2_label_must_be_out_of_platform_ui_and_boxed() -> None:
    ok = manifest(ad_label_layout=2, ad_label_rect=LAYOUT2_RECT, ad_label_box=True)
    assert_render_manifest(ok)
    with pytest.raises(ComplianceError, match="under the platform UI \\(top 150 px\\)"):
        assert_render_manifest(ok.model_copy(update={"ad_label_rect": Rect(x0=38, y0=110, x1=130, y1=190)}))
    with pytest.raises(ComplianceError, match="right 15 %"):
        assert_render_manifest(ok.model_copy(update={"ad_label_rect": Rect(x0=950, y0=536, x1=1046, y1=620)}))
    with pytest.raises(ComplianceError, match="bottom 20 %"):
        assert_render_manifest(ok.model_copy(update={"ad_label_rect": Rect(x0=36, y0=1600, x1=132, y1=1684)}))
    with pytest.raises(ComplianceError, match="no semi-opaque dark background box"):
        assert_render_manifest(ok.model_copy(update={"ad_label_box": False}))
    with pytest.raises(ComplianceError, match="no recorded 'Ad' label position"):
        assert_render_manifest(ok.model_copy(update={"ad_label_rect": None}))


def test_layout2_keeps_the_three_second_rule() -> None:
    with pytest.raises(ComplianceError, match=">= 3.0s"):
        assert_render_manifest(
            manifest(ad_label_layout=2, ad_label_rect=LAYOUT2_RECT, ad_label_box=True, ad_label_end_s=2.5)
        )


def test_older_manifests_keep_their_rules() -> None:
    """Clips rendered before layout 2 (label in the top-left corner) are not re-judged: only timing applies."""
    old = manifest(ad_label_rect=Rect(x0=38, y0=110, x1=130, y1=190))
    assert old.ad_label_layout == 1
    assert_render_manifest(old)
    assert_render_manifest(manifest())  # pre-overlay manifests had no rect at all


def test_new_renders_use_layout3(tmp_path: Path, app_config: AppConfig) -> None:
    """A real render_candidate call (ffmpeg faked) writes label layout 3 (layout 2's rules plus the recorded hook and
    caption geometry), the dark box, and a label rect out of the UI."""
    from clipforge.compliance import AD_LABEL_LAYOUT
    from tests.render.test_clip import Fakes, request

    m = Fakes().render(request(tmp_path), app_config)
    assert AD_LABEL_LAYOUT == 3
    assert m.ad_label_layout == AD_LABEL_LAYOUT
    assert m.hook_rect is not None
    assert m.caption_rect is not None
    assert m.ad_label_box is True
    assert m.ad_label_rect is not None
    assert m.ad_label_rect.y0 >= 150
    assert_render_manifest(m)


def test_layout2_label_never_covered_by_campaign_overlay() -> None:
    base = {"ad_label_layout": 2, "ad_label_rect": LAYOUT2_RECT, "ad_label_box": True}
    assert_render_manifest(manifest(**base, overlay=_overlay(opaque_rect=Rect(x0=266, y0=541, x1=814, y1=673))))
    with pytest.raises(ComplianceError, match="covers the 'Ad' label"):
        assert_render_manifest(manifest(**base, overlay=_overlay(opaque_rect=Rect(x0=0, y0=520, x1=400, y1=640))))


# --- label layout 3 (feat/reframe): stacked / crop / fit layouts --------------------------------------------------

HOOK3 = Rect(x0=0, y0=679, x1=1080, y1=1037)
CAPTIONS3 = Rect(x0=0, y0=1134, x1=1080, y1=1401)
FACE3 = Rect(x0=370, y0=102, x1=710, y1=476)
LABEL3 = Rect(x0=36, y0=158, x1=132, y1=242)


def _layout3(**kw: object) -> RenderManifest:
    data: dict[str, object] = {
        "ad_label_layout": 3,
        "ad_label_rect": LABEL3,
        "ad_label_box": True,
        "layout": "stacked",
        "hook_rect": HOOK3,
        "caption_rect": CAPTIONS3,
        "face_rect": FACE3,
        "facecam_rect": Rect(x0=0, y0=0, x1=1080, y1=640),
    }
    data.update(kw)
    return manifest(**data)


def test_layout3_hook_label_and_overlay_never_cover_the_face() -> None:
    assert_render_manifest(_layout3())
    with pytest.raises(ComplianceError, match="the hook covers the streamer's face"):
        assert_render_manifest(_layout3(hook_rect=Rect(x0=0, y0=167, x1=1080, y1=525)))
    with pytest.raises(ComplianceError, match="the 'Ad' label covers the streamer's face"):
        assert_render_manifest(_layout3(ad_label_rect=Rect(x0=380, y0=200, x1=476, y1=284)))
    with pytest.raises(ComplianceError, match="the campaign overlay covers the streamer's face"):
        assert_render_manifest(_layout3(overlay=_overlay(opaque_rect=Rect(x0=297, y0=400, x1=783, y1=560))))


def test_layout3_overlay_stays_unobstructed() -> None:
    assert_render_manifest(_layout3(overlay=_overlay(opaque_rect=Rect(x0=297, y0=539, x1=783, y1=671))))
    with pytest.raises(ComplianceError, match="the hook covers the campaign overlay"):
        assert_render_manifest(_layout3(overlay=_overlay(opaque_rect=Rect(x0=297, y0=700, x1=783, y1=800))))
    with pytest.raises(ComplianceError, match="the caption band covers the campaign overlay"):
        assert_render_manifest(_layout3(overlay=_overlay(opaque_rect=Rect(x0=297, y0=1200, x1=783, y1=1300))))


def test_layout3_captions_and_hook_stay_out_of_the_platform_ui() -> None:
    with pytest.raises(ComplianceError, match="caption band is under the platform UI \\(bottom 20 %\\)"):
        assert_render_manifest(_layout3(caption_rect=Rect(x0=0, y0=1400, x1=1080, y1=1600)))
    with pytest.raises(ComplianceError, match="hook is under the platform UI \\(top 150 px\\)"):
        assert_render_manifest(_layout3(hook_rect=Rect(x0=0, y0=100, x1=1080, y1=300), face_rect=None, layout="crop"))


def test_layout3_requires_the_recorded_geometry() -> None:
    with pytest.raises(ComplianceError, match="no recorded hook position"):
        assert_render_manifest(_layout3(hook_rect=None))
    with pytest.raises(ComplianceError, match="no recorded caption band"):
        assert_render_manifest(_layout3(caption_rect=None))
    with pytest.raises(ComplianceError, match="stacked layout without a recorded facecam panel and face"):
        assert_render_manifest(_layout3(face_rect=None))


def test_layout3_keeps_the_layout2_label_rules() -> None:
    with pytest.raises(ComplianceError, match="under the platform UI \\(top 150 px\\)"):
        assert_render_manifest(_layout3(ad_label_rect=Rect(x0=36, y0=100, x1=132, y1=184)))
    with pytest.raises(ComplianceError, match=">= 3.0s"):
        assert_render_manifest(_layout3(ad_label_end_s=2.0))
    with pytest.raises(ComplianceError, match="no semi-opaque dark background box"):
        assert_render_manifest(_layout3(ad_label_box=False))


def test_layout2_manifests_are_not_judged_by_layout3_geometry() -> None:
    old = manifest(ad_label_layout=2, ad_label_rect=LAYOUT2_RECT, ad_label_box=True)
    assert old.hook_rect is None
    assert old.layout == "fit"
    assert_render_manifest(old)


@pytest.mark.parametrize("layout", ["stacked", "crop", "fit"])
def test_every_layout_keeps_label_hook_captions_overlay_and_face_apart(
    layout: str, tmp_path: Path, app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real render_candidate (ffmpeg faked), a banner overlay like the first overlay campaign's: in every layout the
    label is out of the UI on its box, and nothing covers the face or the overlay."""
    from clipforge.render import clip as clip_mod
    from clipforge.render.facecam import FacecamSpec, Presence
    from clipforge.schemas import PxBox
    from tests.render.test_clip import Fakes, request
    from tests.support.overlay_png import FAKE_OVERLAY_REL, write_overlay_png

    root = tmp_path / "repo"
    write_overlay_png(root, size=(2172, 724), margin=(100, 140))
    monkeypatch.setattr(clip_mod, "project_root", lambda: root)
    fakes = Fakes()
    fakes.presence = Presence(ratio=0.9, samples=60, face=PxBox(x=172, y=64, w=100, h=110), variation=20.0)
    ov = CampaignOverlay.model_validate(
        {"path": FAKE_OVERLAY_REL, "position": {"x_pct": 50, "y_pct": 31.5}, "scale": 0.6}
    )
    m = fakes.render(
        request(
            tmp_path,
            content_kind="game",
            layout=layout,
            overlay=ov,
            facecam=FacecamSpec(origin="detected", box=PxBox(x=1400, y=700, w=444, h=264)),
        ),
        app_config,
    )
    assert m.layout == layout
    assert_render_manifest(m)
    assert m.ad_label_rect is not None
    assert m.hook_rect is not None
    assert m.caption_rect is not None
    assert m.overlay is not None
    rects = [m.ad_label_rect, m.hook_rect, m.caption_rect, m.overlay.opaque_rect]
    if m.face_rect is not None:
        rects.append(m.face_rect)
    for i, a in enumerate(rects):
        for b in rects[i + 1 :]:
            assert not a.intersects(b), (a, b)


def test_layout3_label_never_overlaps_the_hook_or_captions() -> None:
    with pytest.raises(ComplianceError, match="the 'Ad' label overlaps the hook"):
        assert_render_manifest(_layout3(ad_label_rect=Rect(x0=36, y0=700, x1=132, y1=784)))
    with pytest.raises(ComplianceError, match="the 'Ad' label overlaps the caption band"):
        assert_render_manifest(_layout3(ad_label_rect=Rect(x0=36, y0=1200, x1=132, y1=1284)))
