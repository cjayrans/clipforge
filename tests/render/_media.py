"""Shared markers for tests that need real media tools (skipped on the Mac, run in CI/Docker)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from typing import Any

import pytest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
_skip = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")


def needs_ffmpeg[F: Callable[..., Any]](func: F) -> F:
    marked: F = pytest.mark.needs_ffmpeg(_skip(func))
    return marked
