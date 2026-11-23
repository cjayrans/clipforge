from __future__ import annotations

import pytest

from clipforge.config import AppConfig, RenderConfig
from clipforge.render.ass import ass_document
from clipforge.render.assets import available_fonts, face_model_path, fonts_dir, missing_fonts
from clipforge.render.overlays import (
    AD_LABEL_STYLE,
    HOOK_STYLE,
    ad_label_event,
    hook_event,
    overlay_events,
    overlay_styles,
    wrap_hook,
)


def test_ad_label_timing_from_config(app_config: AppConfig) -> None:
    longer = RenderConfig.model_validate({**app_config.render.model_dump(), "ad_label_seconds": 4.5})
    ev = ad_label_event(longer)
    assert (ev.start_s, ev.end_s, ev.text) == (0.0, 4.5, "Ad")


@pytest.mark.parametrize("hook", ["", "   ", "Watch this"])
def test_ad_label_always_present(app_config: AppConfig, hook: str) -> None:
    events = overlay_events(hook, app_config.render)
    ads = [e for e in events if e.style == AD_LABEL_STYLE]
    assert len(ads) == 1
    assert ads[0].start_s == 0.0
    assert ads[0].end_s >= 3.0
    assert ads[0].text.endswith("}Ad")


def test_hook_spans_hook_seconds(app_config: AppConfig) -> None:
    ev = hook_event("He did NOT expect that", app_config.render)
    assert ev is not None
    assert (ev.start_s, ev.end_s) == (0.0, 2.5)
    assert hook_event("  ", app_config.render) is None


def test_hook_event_in_top_third_and_wrapped(app_config: AppConfig) -> None:
    events = overlay_events("This heist went completely sideways fast", app_config.render)
    hook = next(e for e in events if e.style == HOOK_STYLE)
    assert "\\pos(540,346)" in hook.text
    assert "\\N" in hook.text


def test_hook_escapes_override_tags(app_config: AppConfig) -> None:
    events = overlay_events("{\\fs200}big", app_config.render)
    hook = next(e for e in events if e.style == HOOK_STYLE)
    assert "\\fs200" not in hook.text


def test_wrap_hook() -> None:
    assert wrap_hook("one two three four five six seven eight", 10, 3) == [
        "one two",
        "three four",
        "five six seven eight",
    ]
    assert wrap_hook("short") == ["short"]


def test_overlay_styles_use_preset_font(app_config: AppConfig) -> None:
    for style in app_config.styles.values():
        styles = overlay_styles(style)
        assert {s.name for s in styles} == {AD_LABEL_STYLE, HOOK_STYLE}
        assert all(s.font == style.font for s in styles)
        ass_document(styles, overlay_events("hook", app_config.render))  # styles cover events


def test_every_preset_font_is_bundled(app_config: AppConfig) -> None:
    fonts = {s.font for s in app_config.styles.values()}
    assert missing_fonts(fonts) == []
    names = available_fonts()
    assert names["Montserrat-ExtraBold"].name == "Montserrat-ExtraBold.ttf"
    assert names["Inter-Black"].name == "Inter-Black.ttf"
    assert missing_fonts(["Comic-Sans-Nope"]) == ["Comic-Sans-Nope"]


def test_fonts_have_licence_files() -> None:
    assert (fonts_dir() / "Montserrat-OFL.txt").is_file()
    assert (fonts_dir() / "Inter-OFL.txt").is_file()


def test_face_model_bundled() -> None:
    assert face_model_path().stat().st_size == 229_746
