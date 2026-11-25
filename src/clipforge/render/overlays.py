"""Hook text and "Ad" label overlays (ARCHITECTURE §4.4; COMPLIANCE invariant 3).

- "Ad" label: left edge, just below the hook's largest possible box (out of the platform UI zones: TikTok's top
  150 px, right 15 %, bottom 20 %), on a semi-opaque dark box so it reads on bright or busy footage. From 0.0 s to
  `render.ad_label_seconds` (config validation keeps it ≥ 3.0 s), text `render.ad_label_text`. Always present,
  whatever the hook. `render.campaign_overlay.place_ad_label` picks the spot and checks it.
- Hook: top third, 0 → `render.hook_seconds`, wrapped to short lines, in the account's style-preset font.

Both are ASS events burned in by the same `ass` filter as the captions; the encoder records them in the
`schemas.RenderManifest` (`has_ad_label`, `ad_label_start_s`, `ad_label_end_s`, `hook_text`, `hook_end_s`).
"""

from __future__ import annotations

from dataclasses import dataclass

from clipforge.config import RenderConfig, Style
from clipforge.render.ass import AssEvent, AssStyle, ass_colour, escape_text

AD_LABEL_STYLE = "AdLabel"
HOOK_STYLE = "Hook"
AD_LABEL_POS = (48, 548)  # left edge, under the hook's worst case (y <= 525) and out of the top UI zone
AD_LABEL_SIZE = 48
AD_LABEL_PAD = 12  # box padding (BorderStyle 3 uses the outline width as padding)
AD_LABEL_BOX_ALPHA = 0x40  # ASS alpha of the box: 0x00 opaque .. 0xFF clear; 0x40 = 75 % opaque black
HOOK_Y_PCT = 18.0  # top third
HOOK_LINE_CHARS = 16
HOOK_MAX_LINES = 3
AD_LABEL_LAYER = 10
HOOK_LAYER = 5


@dataclass(frozen=True)
class OverlayEvent:
    start_s: float
    end_s: float
    text: str


def ad_label_event(cfg: RenderConfig) -> OverlayEvent:
    """Invariant 3: the "Ad" label spans 0.0 s to ad_label_seconds (≥ 3.0 s)."""
    return OverlayEvent(start_s=0.0, end_s=cfg.ad_label_seconds, text=cfg.ad_label_text)


def hook_event(hook: str, cfg: RenderConfig) -> OverlayEvent | None:
    text = escape_text(hook)
    if not text:
        return None
    return OverlayEvent(start_s=0.0, end_s=cfg.hook_seconds, text=text)


def wrap_hook(text: str, line_chars: int = HOOK_LINE_CHARS, max_lines: int = HOOK_MAX_LINES) -> list[str]:
    """Greedy word wrap; words beyond max_lines go on the last line (hooks are ≤ 8 words, so this is rare)."""
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= line_chars:
            lines[-1] += " " + word
        elif len(lines) < max_lines:
            lines.append(word)
        else:
            lines[-1] += " " + word
    return lines


def ad_label_style(font: str) -> AssStyle:
    """White text on a semi-opaque dark box (BorderStyle 3: libass fills the box with the outline colour)."""
    dark = ass_colour("#000000", alpha=AD_LABEL_BOX_ALPHA)
    return AssStyle(
        name=AD_LABEL_STYLE,
        font=font,
        size=AD_LABEL_SIZE,
        primary=ass_colour("#FFFFFF"),
        outline_colour=dark,
        back_colour=dark,
        border_style=3,
        outline=AD_LABEL_PAD,
        shadow=0,
        alignment=7,
    )


def has_dark_box(style: AssStyle) -> bool:
    """True if the style draws an opaque-enough dark box behind white text (alpha <= 0x60, i.e. >= 62 % opaque)."""
    box = style.outline_colour  # &HAABBGGRR
    alpha, rgb = int(box[2:4], 16), box[4:]
    return (
        style.border_style == 3
        and style.outline >= 8
        and alpha <= 0x60
        and max(int(rgb[i : i + 2], 16) for i in (0, 2, 4)) <= 0x30
        and style.primary.endswith("FFFFFF")
    )


def overlay_styles(style: Style) -> list[AssStyle]:
    box = style.box or "#000000"
    return [
        ad_label_style(style.font),
        AssStyle(
            name=HOOK_STYLE,
            font=style.font,
            size=round(style.size * 1.1),
            primary=ass_colour(style.primary if style.box else "#FFFFFF"),
            outline_colour=ass_colour(box, alpha=0 if style.box else 0x30),
            back_colour=ass_colour(box, alpha=0 if style.box else 0x30),
            border_style=3,
            outline=18,
            alignment=5,
        ),
    ]


def overlay_events(
    hook: str,
    cfg: RenderConfig,
    play_res: tuple[int, int] = (1080, 1920),
    ad_label_at: tuple[int, int, int] = (7, *AD_LABEL_POS),
    hook_cy: int | None = None,
) -> list[AssEvent]:
    """`ad_label_at` = (ASS alignment, x, y) and `hook_cy` (the hook's centre y) come from
    render.campaign_overlay.place_texts, which keeps them clear of a campaign overlay and, in the stacked layout,
    of the streamer's face."""
    ad = ad_label_event(cfg)
    align, x, y = ad_label_at
    events = [
        AssEvent(
            ad.start_s, ad.end_s, AD_LABEL_STYLE, f"{{\\an{align}\\pos({x},{y})}}{escape_text(ad.text)}", AD_LABEL_LAYER
        )
    ]
    h = hook_event(hook, cfg)
    if h is not None:
        cy = round(HOOK_Y_PCT / 100 * play_res[1]) if hook_cy is None else hook_cy
        pos = f"{{\\an5\\pos({play_res[0] // 2},{cy})}}"
        events.append(AssEvent(h.start_s, h.end_s, HOOK_STYLE, pos + "\\N".join(wrap_hook(h.text)), HOOK_LAYER))
    return events
