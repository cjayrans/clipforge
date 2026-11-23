"""Minimal pure ASS (Advanced SubStation Alpha v4+) writer, used for captions and overlays (burned by libass).

Format reference: the ASS v4+ spec as implemented by libass — `[Script Info]`, `[V4+ Styles]` with the standard
23-field `Style:` format line, `[Events]` with the 10-field `Dialogue:` format line; colours are `&HAABBGGRR`
(alpha 00 = opaque), times are `H:MM:SS.cc`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

PLAY_RES = (1080, 1920)

STYLE_FIELDS = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
    "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, "
    "Encoding"
)
EVENT_FIELDS = "Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def ass_colour(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (alpha 0 = opaque, 255 = transparent)."""
    m = _HEX_RE.match(hex_rgb.strip())
    if not m:
        raise ValueError(f"colour must be #RRGGBB, got {hex_rgb!r}")
    if not 0 <= alpha <= 255:
        raise ValueError("alpha must be 0-255")
    rgb = m.group(1).upper()
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def inline_colour(hex_rgb: str) -> str:
    """Colour for an inline `\\c` override: '&HBBGGRR&'."""
    return f"&H{ass_colour(hex_rgb)[4:]}&"


def ass_time(seconds: float) -> str:
    """Seconds -> 'H:MM:SS.cc' (centiseconds, rounded)."""
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_text(text: str) -> str:
    """Make arbitrary text safe inside a Dialogue line: no override blocks, no escapes, one line."""
    return " ".join(text.replace("\\", "/").replace("{", "(").replace("}", ")").split())


@dataclass(frozen=True)
class AssStyle:
    name: str
    font: str
    size: int
    primary: str = "&H00FFFFFF"
    secondary: str = "&H00FFFFFF"
    outline_colour: str = "&H00000000"
    back_colour: str = "&H00000000"
    bold: bool = True
    border_style: int = 1  # 1 = outline + shadow, 3 = opaque box
    outline: float = 0
    shadow: float = 0
    alignment: int = 5  # numpad layout; 5 = middle centre
    margin_l: int = 0
    margin_r: int = 0
    margin_v: int = 0

    def line(self) -> str:
        fields = [
            self.name,
            self.font,
            str(self.size),
            self.primary,
            self.secondary,
            self.outline_colour,
            self.back_colour,
            "-1" if self.bold else "0",
            "0",
            "0",
            "0",
            "100",
            "100",
            "0",
            "0",
            str(self.border_style),
            _num(self.outline),
            _num(self.shadow),
            str(self.alignment),
            str(self.margin_l),
            str(self.margin_r),
            str(self.margin_v),
            "1",
        ]
        return "Style: " + ",".join(fields)


@dataclass(frozen=True)
class AssEvent:
    start_s: float
    end_s: float
    style: str
    text: str  # already escaped; may contain override tags
    layer: int = 0

    def line(self) -> str:
        return (
            f"Dialogue: {self.layer},{ass_time(self.start_s)},{ass_time(self.end_s)},{self.style},,0,0,0,,{self.text}"
        )


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def ass_document(styles: Iterable[AssStyle], events: Iterable[AssEvent], play_res: tuple[int, int] = PLAY_RES) -> str:
    styles = list(styles)
    names = [s.name for s in styles]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate ASS style names: {names}")
    events = list(events)
    unknown = {e.style for e in events} - set(names)
    if unknown:
        raise ValueError(f"events use undefined styles: {sorted(unknown)}")
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_res[0]}",
        f"PlayResY: {play_res[1]}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        f"Format: {STYLE_FIELDS}",
        *(s.line() for s in styles),
        "",
        "[Events]",
        f"Format: {EVENT_FIELDS}",
        *(e.line() for e in sorted(events, key=lambda e: (e.start_s, e.layer))),
        "",
    ]
    return "\n".join(lines)
