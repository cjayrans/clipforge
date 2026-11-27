"""Disk-space guard before a download (moments-v2): whole VODs are tens of GB.

`check_free(directory, need_bytes, min_free_gb)` raises `DiskSpaceError` (a DownloadError: the source stays
REGISTERED, the operator gets an alert, and the download is tried again next night) unless the volume holding
`directory` has at least the estimated size plus `min_free_gb` free. `estimate_bytes` reads yt-dlp metadata.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

GB = 1024**3


def free_bytes(directory: Path) -> int:
    """Free bytes on the volume holding `directory` (or its nearest existing parent)."""
    path = directory
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def estimate_bytes(meta: dict[str, Any]) -> int | None:
    """Expected download size from yt-dlp metadata: filesize, filesize_approx, else duration x total bitrate."""
    for key in ("filesize", "filesize_approx"):
        value = meta.get(key)
        if isinstance(value, int | float) and value > 0:
            return int(value)
    duration, tbr = meta.get("duration"), meta.get("tbr")
    if isinstance(duration, int | float) and isinstance(tbr, int | float) and duration > 0 and tbr > 0:
        return int(duration * tbr * 1000 / 8)  # tbr is kbit/s
    return None


def check_free(directory: Path, need_bytes: int | None, min_free_gb: float) -> int:
    """Raise DiskSpaceError unless `need_bytes` (0 when unknown) plus `min_free_gb` is free. Returns free bytes."""
    from clipforge.ingest.download import DiskSpaceError

    free = free_bytes(directory)
    need = (need_bytes or 0) + int(min_free_gb * GB)
    if free < need:
        size = f"about {need_bytes / GB:.1f} GB" if need_bytes else "unknown size"
        raise DiskSpaceError(
            f"not enough disk space: {free / GB:.1f} GB free, need {need / GB:.1f} GB ({size} plus {min_free_gb:g} GB "
            "headroom); it is tried again next night"
        )
    return free
