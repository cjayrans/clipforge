"""Word-highlight captions as ASS (pure; ARCHITECTURE §4.4, PRD F5).

- `chunk_words`: transcript words inside the clip → clip-relative chunks of 2–4 words, at most 18 characters per
  line (a single longer word stands alone). Chunks also break after sentence punctuation and at pauses.
- `caption_events`: one Dialogue event per word; the whole chunk is shown with the active word in the highlight
  colour (underlined when the preset's highlight equals its primary colour, e.g. boxed-black).
- `caption_style`: config `styles[<preset>]` → ASS style. Vertical position is `y_pct` clamped to the safe zone
  62–70 % of the height (clear of platform UI in the bottom ~20 %). A `box` colour gives an opaque box
  (BorderStyle 3); otherwise an outline (default black).
- Optional preset keys (Style allows extras): `uppercase` (default true), `outline_colour` (default #000000),
  `shadow` (default 0).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.config import Style
from clipforge.render.ass import PLAY_RES, AssEvent, AssStyle, ass_colour, escape_text, inline_colour
from clipforge.schemas import Word

CAPTION_STYLE = "Caption"
MIN_WORDS = 2
MAX_WORDS = 4
MAX_LINE_CHARS = 18
PAUSE_S = 0.6
SAFE_ZONE_PCT = (62.0, 70.0)
LINGER_S = 0.25
_BREAK_AFTER = (".", "?", "!", ",", ";", ":", "…")


@dataclass(frozen=True)
class TimedWord:
    text: str
    start_s: float  # clip-relative
    end_s: float


@dataclass(frozen=True)
class Chunk:
    words: tuple[TimedWord, ...]

    @property
    def start_s(self) -> float:
        return self.words[0].start_s

    @property
    def end_s(self) -> float:
        return self.words[-1].end_s

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def clip_words(words: Sequence[Word], clip_start_s: float, clip_end_s: float) -> list[TimedWord]:
    """Words overlapping [clip_start, clip_end], shifted to clip time and clamped to the clip."""
    length = clip_end_s - clip_start_s
    out: list[TimedWord] = []
    for w in sorted(words, key=lambda w: w.start_s):
        text = escape_text(w.text)
        if not text or w.end_s <= clip_start_s or w.start_s >= clip_end_s:
            continue
        start = min(max(0.0, w.start_s - clip_start_s), length)
        end = min(max(start, w.end_s - clip_start_s), length)
        out.append(TimedWord(text, start, end))
    return out


def chunk_words(words: Sequence[TimedWord]) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[TimedWord] = []

    def flush() -> None:
        if current:
            chunks.append(Chunk(tuple(current)))
            current.clear()

    for word in words:
        if current:
            prev = current[-1]
            joined = len(" ".join([*(w.text for w in current), word.text]))
            if (
                len(current) >= MAX_WORDS
                or joined > MAX_LINE_CHARS
                or (len(current) >= MIN_WORDS and prev.text.endswith(_BREAK_AFTER))
                or word.start_s - prev.end_s > PAUSE_S
            ):
                flush()
        current.append(word)
    flush()
    return chunks


def safe_y(y_pct: float, height: int = PLAY_RES[1]) -> int:
    lo, hi = SAFE_ZONE_PCT
    return round(min(max(y_pct, lo), hi) / 100 * height)


def caption_style(style: Style) -> AssStyle:
    extra = style.model_extra or {}
    outline_hex = str(extra.get("outline_colour", "#000000"))
    if style.box:
        return AssStyle(
            name=CAPTION_STYLE,
            font=style.font,
            size=style.size,
            primary=ass_colour(style.primary),
            outline_colour=ass_colour(style.box),
            back_colour=ass_colour(style.box),
            border_style=3,
            outline=max(8, style.outline or 0),  # box padding
            shadow=0,
        )
    return AssStyle(
        name=CAPTION_STYLE,
        font=style.font,
        size=style.size,
        primary=ass_colour(style.primary),
        outline_colour=ass_colour(outline_hex),
        back_colour=ass_colour("#000000", alpha=128),
        border_style=1,
        outline=style.outline if style.outline is not None else 4,
        shadow=float(extra.get("shadow", 0)),
    )


def _display(text: str, uppercase: bool) -> str:
    return text.upper() if uppercase else text


def caption_events(
    chunks: Sequence[Chunk], style: Style, clip_len_s: float | None = None, play_res: tuple[int, int] = PLAY_RES
) -> list[AssEvent]:
    extra = style.model_extra or {}
    uppercase = bool(extra.get("uppercase", True))
    pos = f"\\an5\\pos({play_res[0] // 2},{safe_y(style.y_pct, play_res[1])})"
    if style.highlight.lower() == style.primary.lower():
        on, off = "{\\u1}", "{\\u0}"
    else:
        on, off = "{\\c" + inline_colour(style.highlight) + "}", "{\\c" + inline_colour(style.primary) + "}"

    events: list[AssEvent] = []
    for ci, chunk in enumerate(chunks):
        next_start = chunks[ci + 1].start_s if ci + 1 < len(chunks) else None
        chunk_end = chunk.end_s + LINGER_S
        if next_start is not None:
            chunk_end = min(chunk_end, next_start)
        chunk_end = max(chunk_end, chunk.end_s)
        if clip_len_s is not None:
            chunk_end = min(chunk_end, clip_len_s)
        for wi, word in enumerate(chunk.words):
            start = word.start_s
            end = chunk.words[wi + 1].start_s if wi + 1 < len(chunk.words) else chunk_end
            if end <= start:
                continue
            parts = [
                (on + _display(w.text, uppercase) + off) if i == wi else _display(w.text, uppercase)
                for i, w in enumerate(chunk.words)
            ]
            events.append(AssEvent(start, end, CAPTION_STYLE, "{" + pos + "}" + " ".join(parts)))
    return events


def build_captions(
    words: Sequence[Word], clip_start_s: float, clip_end_s: float, style: Style
) -> tuple[AssStyle, list[AssEvent]]:
    """Caption style and events for one clip (the encoder merges them with the overlay events)."""
    chunks = chunk_words(clip_words(words, clip_start_s, clip_end_s))
    return caption_style(style), caption_events(chunks, style, clip_end_s - clip_start_s)
