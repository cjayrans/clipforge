"""Locate bundled assets (`assets/fonts`, `assets/models`) at the repo root.

Resolution order: the repo checkout this module lives in (editable installs, tests, CI), then `<cwd>/assets`
(the Docker image runs from /app; see docs/contract-requests/render.md for the `COPY assets` request).
"""

from __future__ import annotations

from pathlib import Path

_REPO_ASSETS = Path(__file__).resolve().parents[3] / "assets"

FACE_MODEL_NAME = "blaze_face_short_range.tflite"


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
