"""Locate bundled assets (`assets/fonts`, `assets/models`) at the repo root, and check fonts by name.

Resolution order: the repo checkout this module lives in (editable installs, tests, CI), then `<cwd>/assets`
(the Docker image runs from /app; see docs/contract-requests/render.md for the `COPY assets` request).

libass looks fonts up in `fontsdir` by family, full or PostScript name. Config `styles.*.font` uses PostScript
names (e.g. `Montserrat-ExtraBold`); `font_names` reads them from the TTF `name` table so a typo fails loudly
instead of libass silently falling back to another font.
"""

from __future__ import annotations

import struct
from collections.abc import Iterable
from pathlib import Path

_REPO_ASSETS = Path(__file__).resolve().parents[3] / "assets"

FACE_MODEL_NAME = "blaze_face_short_range.tflite"
_NAME_IDS = (1, 4, 6)  # family, full name, PostScript name


class AssetNotFoundError(FileNotFoundError):
    pass


def assets_dir() -> Path:
    for candidate in (_REPO_ASSETS, Path.cwd() / "assets"):
        if candidate.is_dir():
            return candidate
    raise AssetNotFoundError(f"assets/ not found (looked in {_REPO_ASSETS} and {Path.cwd() / 'assets'})")


def fonts_dir() -> Path:
    return assets_dir() / "fonts"


def face_model_path() -> Path:
    path = assets_dir() / "models" / FACE_MODEL_NAME
    if not path.is_file():
        raise AssetNotFoundError(f"face detector model missing: {path}")
    return path


def font_names(data: bytes) -> set[str]:
    """Family, full and PostScript names from a TrueType/OpenType `name` table."""
    num_tables = struct.unpack_from(">H", data, 4)[0]
    name_offset = None
    for i in range(num_tables):
        tag, _checksum, offset, _length = struct.unpack_from(">4sIII", data, 12 + 16 * i)
        if tag == b"name":
            name_offset = offset
            break
    if name_offset is None:
        raise ValueError("font has no name table")
    _fmt, count, string_offset = struct.unpack_from(">HHH", data, name_offset)
    names: set[str] = set()
    for i in range(count):
        platform, _enc, _lang, name_id, length, offset = struct.unpack_from(">HHHHHH", data, name_offset + 6 + 12 * i)
        if name_id not in _NAME_IDS:
            continue
        raw = data[name_offset + string_offset + offset : name_offset + string_offset + offset + length]
        text = raw.decode("utf-16-be", errors="replace") if platform in (0, 3) else raw.decode("latin-1")
        names.add(text)
    return names


def available_fonts(directory: Path | None = None) -> dict[str, Path]:
    """Every family/full/PostScript name in the fonts directory → its file."""
    out: dict[str, Path] = {}
    for path in sorted((directory or fonts_dir()).glob("*.[ot]tf")):
        for name in font_names(path.read_bytes()):
            out.setdefault(name, path)
    return out


def missing_fonts(fonts: Iterable[str], directory: Path | None = None) -> list[str]:
    have = available_fonts(directory)
    return sorted({f for f in fonts if f not in have})
