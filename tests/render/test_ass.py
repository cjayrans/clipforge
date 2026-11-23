from __future__ import annotations

import pytest

from clipforge.render.ass import AssEvent, AssStyle, ass_colour, ass_document, ass_time, escape_text, inline_colour


@pytest.mark.parametrize(
    ("hex_rgb", "alpha", "out"),
    [("#FFD400", 0, "&H0000D4FF"), ("#000000", 128, "&H80000000"), ("7cdbff", 0, "&H00FFDB7C")],
)
def test_ass_colour_is_aabbggrr(hex_rgb: str, alpha: int, out: str) -> None:
    assert ass_colour(hex_rgb, alpha) == out


def test_inline_colour() -> None:
    assert inline_colour("#FFD400") == "&H00D4FF&"


@pytest.mark.parametrize("bad", ["#FFF", "red", "#GGGGGG"])
def test_ass_colour_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValueError, match="#RRGGBB"):
        ass_colour(bad)


def test_ass_colour_rejects_bad_alpha() -> None:
    with pytest.raises(ValueError, match="alpha"):
        ass_colour("#FFFFFF", 300)


@pytest.mark.parametrize(
    ("s", "out"),
    [(0, "0:00:00.00"), (2.5, "0:00:02.50"), (61.234, "0:01:01.23"), (3725.0, "1:02:05.00"), (-1, "0:00:00.00")],
)
def test_ass_time(s: float, out: str) -> None:
    assert ass_time(s) == out


def test_escape_text_removes_overrides_and_newlines() -> None:
    assert escape_text("a {\\b1}bold\\N\nline  ") == "a (/b1)bold/N line"


def test_document_structure() -> None:
    style = AssStyle(name="S", font="Inter-Bold", size=70)
    doc = ass_document([style], [AssEvent(1.0, 2.0, "S", "hi"), AssEvent(0.0, 1.0, "S", "first")])
    assert "PlayResX: 1080" in doc
    assert "PlayResY: 1920" in doc
    assert "Style: S,Inter-Bold,70," in doc
    dialogue = [line for line in doc.splitlines() if line.startswith("Dialogue:")]
    assert dialogue == [
        "Dialogue: 0,0:00:00.00,0:00:01.00,S,,0,0,0,,first",
        "Dialogue: 0,0:00:01.00,0:00:02.00,S,,0,0,0,,hi",
    ]
    # Style line has exactly the 23 declared fields.
    style_line = next(line for line in doc.splitlines() if line.startswith("Style:"))
    assert len(style_line.removeprefix("Style: ").split(",")) == 23


def test_document_rejects_undefined_or_duplicate_styles() -> None:
    s = AssStyle(name="S", font="F", size=10)
    with pytest.raises(ValueError, match="undefined"):
        ass_document([s], [AssEvent(0, 1, "X", "t")])
    with pytest.raises(ValueError, match="duplicate"):
        ass_document([s, s], [])
