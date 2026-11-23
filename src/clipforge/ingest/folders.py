"""Folder links (PRD F17): list the files of a campaign's Google Drive folder so each is registered as a source.

Only the folder link the operator registers is read, and each listed file becomes its own `sources` row
(`https://drive.google.com/file/d/<id>/view`, with `folder_url` set). Downloads still go through
`download_source`, one registered file at a time; each file is hashed when downloaded.

gdown facts (checked against the installed gdown 6.5.2 source, https://github.com/wkentaro/gdown):
`download_folder(url=..., skip_download=True)` returns `GoogleDriveFileToDownload(id, path, local_path)` for every
file, recursing into subfolders, and creates no directories when `skip_download=True`.

Dropbox folder links are refused in `registry.refusal_reason`: listing one needs a Dropbox API token, which v1 does not
have (decision recorded in docs/pr-descriptions/ingest-transcribe.md).
"""

from __future__ import annotations

import logging
from pathlib import PurePosixPath

from clipforge import compliance

log = logging.getLogger(__name__)


class FolderListError(RuntimeError):
    """The folder could not be listed (not shared publicly, network error, ...)."""


def gdrive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def is_media_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in compliance.MEDIA_EXTENSIONS


def list_gdrive_folder(folder_url: str) -> list[tuple[str, str]]:
    """[(file_url, relative path)] for every media file in the Drive folder (recursive), sorted by path."""
    import gdown

    try:
        files = gdown.download_folder(url=folder_url, skip_download=True, quiet=True)
    except Exception as exc:  # gdown raises its own errors and requests errors
        raise FolderListError(f"{type(exc).__name__}: {exc}") from exc
    if not files:
        raise FolderListError("folder is empty or not shared with 'Anyone with the link'")
    out: list[tuple[str, str]] = []
    for f in files:
        file_id = getattr(f, "id", None)
        path = str(getattr(f, "path", ""))
        if not file_id or not is_media_path(path):
            log.info("skipping non-media file in Drive folder: %s", path)
            continue
        out.append((gdrive_file_url(str(file_id)), path))
    return sorted(out, key=lambda t: t[1])
