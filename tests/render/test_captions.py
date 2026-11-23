from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.config import AppConfig, Style
from clipforge.render.captions import (
    CAPTION_STYLE,
    MAX_LINE_CHARS,
    MAX_WORDS,
    Chunk,
    TimedWord,
    build_captions,
    caption_events,
    caption_style,
    chunk_words,
    clip_words,
    safe_y,
)
from clipforge.schemas import Transcript, Word
from tests.conftest import FIXTURES


def tw(text: str, start: float, end: float | None = None) -> TimedWord:
    return TimedWord(text, start, start + 0.3 if end is None else end)


def test_clip_words_shifts_and_clamps() -> None:
    words = [Word(text="before", start_s=1, end_s=2), Word(text="edge", start_s=9.8, end_s=10.4),
             Word(text="in", start_s=12, end_s=12.5), Word(text="tail", start_s=19.9, end_s=20.6),
             Word(text="after", start_s=21, end_s=22), Word(text=" ", start_s=13, end_s=13.1)]  # fmt: skip
    out = clip_words(words, 10.0, 20.0)
    assert [w.text for w in out] == ["edge", "in", "tail"]
    assert out[0].start_s == 0.0
    assert out[1].start_s == pytest.approx(2.0)
    assert out[2].end_s == pytest.approx(10.0)


def test_chunk_breaks_at_four_words() -> None:
    words = [tw(t, i * 0.3) for i, t in enumerate(["a", "b", "c", "d", "e", "f"])]
    assert [c.text for c in chunk_words(words)] == ["a b c d", "e f"]


def test_chunk_breaks_at_line_length() -> None:
    words = [tw("incredible", 0), tw("absolutely", 0.3), tw("wow", 0.6)]
    assert [c.text for c in chunk_words(words)] == ["incredible", "absolutely wow"]


def test_chunk_breaks_after_punctuation_and_pause() -> None:
    words = [tw("no", 0), tw("way.", 0.3), tw("he", 0.6), tw("did", 0.9), tw("it", 2.5)]
    assert [c.text for c in chunk_words(words)] == ["no way.", "he did", "it"]


def test_single_long_word_stands_alone() -> None:
    assert [c.text for c in chunk_words([tw("supercalifragilistic", 0)])] == ["supercalifragilistic"]


@given(st.lists(st.text(alphabet="abcdefghij", min_size=1, max_size=12), min_size=1, max_size=60))
def test_chunk_invariants(texts: list[str]) -> None:
    words = [tw(t, i * 0.35) for i, t in enumerate(texts)]
    chunks = chunk_words(words)
    assert [w for c in chunks for w in c.words] == words  # order kept, nothing dropped
    for c in chunks:
        assert 1 <= len(c.words) <= MAX_WORDS
        assert len(c.words) == 1 or len(c.text) <= MAX_LINE_CHARS


@pytest.mark.parametrize(("pct", "y"), [(50, 1190), (66, 1267), (64, 1229), (90, 1344)])
def test_safe_zone_clamp(pct: float, y: int) -> None:
    assert safe_y(pct) == y


def style(**kw: object) -> Style:
    base: dict[str, object] = {"font": "Inter-Bold", "size": 72, "primary": "#FFFFFF", "highlight": "#7CDBFF",
                               "outline": 5, "y_pct": 64}  # fmt: skip
    base.update(kw)
    return Style.model_validate(base)


def test_word_highlight_one_event_per_word() -> None:
    chunk = Chunk((tw("big", 0.0, 0.3), tw("win", 0.4, 0.8)))
    events = caption_events([chunk], style())
    assert [(e.start_s, e.end_s) for e in events] == [(0.0, 0.4), (0.4, pytest.approx(1.05))]
    assert all(e.style == CAPTION_STYLE for e in events)
    assert events[0].text == "{\\an5\\pos(540,1229)}{\\c&HFFDB7C&}BIG{\\c&HFFFFFF&} WIN"
    assert events[1].text.endswith("BIG {\\c&HFFDB7C&}WIN{\\c&HFFFFFF&}")


def test_chunk_end_never_overlaps_next_chunk() -> None:
    chunks = [Chunk((tw("a", 0, 0.5),)), Chunk((tw("b", 0.6, 0.9),))]
    events = caption_events(chunks, style())
    assert events[0].end_s == 0.6


def test_last_chunk_clamped_to_clip_length() -> None:
    events = caption_events([Chunk((tw("end", 9.5, 9.9),))], style(), clip_len_s=10.0)
    assert events[0].end_s == 10.0


def test_uppercase_can_be_disabled() -> None:
    events = caption_events([Chunk((tw("Hi", 0),))], style(uppercase=False))
    assert "Hi" in events[0].text


def test_highlight_equal_to_primary_uses_underline() -> None:
    events = caption_events([Chunk((tw("a", 0), tw("b", 0.3)))], style(primary="#000000", highlight="#000000"))
    assert "{\\u1}A{\\u0}" in events[0].text


def test_presets_map_to_ass_styles(app_config: AppConfig) -> None:
    by_name = {name: caption_style(s) for name, s in app_config.styles.items()}
    assert set(by_name) == {"bold-yellow", "clean-white", "boxed-black"}
    bold = by_name["bold-yellow"]
    assert (bold.font, bold.size, bold.border_style, bold.outline) == ("Montserrat-ExtraBold", 78, 1, 6)
    assert bold.outline_colour == "&H00000000"
    clean = by_name["clean-white"]
    assert (clean.font, clean.border_style, clean.outline) == ("Inter-Bold", 1, 5)
    boxed = by_name["boxed-black"]
    assert (boxed.font, boxed.border_style) == ("Inter-Black", 3)
    assert boxed.back_colour == boxed.outline_colour == "&H00FFFFFF"
    assert boxed.primary == "&H00000000"


def test_build_captions_on_fixture_transcript(app_config: AppConfig) -> None:
    transcript = Transcript.model_validate(json.loads((FIXTURES / "transcript_talk.json").read_text()))
    ass_style, events = build_captions(transcript.words, 20.0, 50.0, app_config.styles["bold-yellow"])
    assert ass_style.name == CAPTION_STYLE
    assert events
    assert all(0.0 <= e.start_s < e.end_s <= 30.0 for e in events)
    starts = [e.start_s for e in events]
    assert starts == sorted(starts)
    for a, b in zip(events, events[1:], strict=False):
        assert a.end_s <= b.start_s + 1e-9  # one caption on screen at a time
