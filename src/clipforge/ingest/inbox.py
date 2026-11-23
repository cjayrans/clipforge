"""`inbox/<campaign_slug>/` drop folder (ARCHITECTURE §4.1, PRD F2).

The operator drops footage a campaign supplied (downloaded by hand) into `inbox/<slug>/`. `scan_inbox` registers each
media file as a source of that campaign with `url = file://inbox/<slug>/<relative path>`, after confirming the
campaign exists and is active. `download_source` then calls `take`, which moves the file into the media directory.

Safety: files are only taken from the folder of the campaign that owns the source (footage is licensed per campaign),
paths must resolve inside that folder (no `..`, no symlinks), and files modified in the last minute are left alone
because they may still be copying.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge import compliance
from clipforge.compliance import ComplianceError
from clipforge.config import Settings
from clipforge.db import CampaignRow, SourceRow
from clipforge.ingest import alerts
from clipforge.schemas import CampaignStatus, SourceHost

log = logging.getLogger(__name__)

MIN_FILE_AGE_S = 60.0
_SKIP_SUFFIXES = (".part", ".crdownload", ".download", ".tmp")


def inbox_url(slug: str, rel_path: str) -> str:
    return f"file://inbox/{slug}/{quote(rel_path)}"


def parse_inbox_url(url: str) -> tuple[str, str]:
    """`file://inbox/<slug>/<rel>` -> (slug, rel). Raises ComplianceError for anything else."""
    parts = urlsplit(url)
    if parts.scheme != "file" or parts.netloc != "inbox":
        raise ComplianceError(1, f"not an inbox URL: {url}")
    slug, _, rel = unquote(parts.path).lstrip("/").partition("/")
    if not slug or not rel:
        raise ComplianceError(1, f"inbox URL needs a campaign folder and a file: {url}")
    return slug, rel


def resolve_inbox_file(settings: Settings, slug: str, rel: str) -> Path:
    """The file for (slug, rel), guaranteed to be inside `inbox/<slug>/` and not a symlink."""
    folder = settings.inbox_dir / slug
    if folder.is_symlink():
        raise ComplianceError(1, f"inbox folder is a symlink: {slug}/")
    base = folder.resolve()
    candidate = folder / rel
    if candidate.is_symlink():
        raise ComplianceError(1, f"inbox file is a symlink: {slug}/{rel}")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base) or resolved == base:
        raise ComplianceError(1, f"inbox path escapes the campaign folder: {slug}/{rel}")
    return resolved


def take(settings: Settings, campaign: CampaignRow, url: str, dest_stem: Path) -> Path:
    """Move the inbox file for `url` to `dest_stem.<ext>` and return the new path."""
    from clipforge.ingest.download import DownloadError

    slug, rel = parse_inbox_url(url)
    if slug != campaign.slug:
        raise ComplianceError(1, f"inbox/{slug}/ belongs to another campaign than {campaign.slug!r}")
    src = resolve_inbox_file(settings, slug, rel)
    if not src.is_file():
        raise DownloadError(f"inbox file is gone: inbox/{slug}/{rel}")
    dest = dest_stem.with_name(dest_stem.name + src.suffix.lower())
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), dest)
    log.info("took inbox/%s/%s -> %s", slug, rel, dest)
    return dest


def _is_candidate_file(path: Path, now: float, min_age_s: float) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if any(part.startswith(".") for part in path.parts[-1:]) or path.name.lower().endswith(_SKIP_SUFFIXES):
        return False
    if path.suffix.lower() not in compliance.MEDIA_EXTENSIONS:
        return False
    return now - path.stat().st_mtime >= min_age_s


def scan_inbox(
    session: Session, settings: Settings, *, now: float | None = None, min_age_s: float = MIN_FILE_AGE_S
) -> list[SourceRow]:
    """Register new media files under `inbox/<slug>/` for active campaigns. Returns the newly registered rows."""
    from clipforge.ingest.registry import register_source

    root = settings.inbox_dir
    if not root.is_dir():
        return []
    now = time.time() if now is None else now
    created_rows: list[SourceRow] = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")):
        campaign = session.scalar(select(CampaignRow).where(CampaignRow.slug == folder.name))
        files = sorted(p for p in folder.rglob("*") if not any(x.startswith(".") for x in p.relative_to(folder).parts))
        files = [p for p in files if _is_candidate_file(p, now, min_age_s)]
        if not files:
            continue
        if campaign is None:
            alerts.alert_operator(
                session,
                alerts.INBOX_UNKNOWN,
                f"inbox:{folder.name}",
                f"inbox/{folder.name}/ has {len(files)} file(s) but no campaign has that slug",
            )
            continue
        if campaign.status is not CampaignStatus.ACTIVE:
            alerts.alert_operator(
                session,
                alerts.CAMPAIGN_INACTIVE,
                f"campaign:{campaign.id}",
                f"inbox/{folder.name}/ has {len(files)} file(s) but the campaign is {campaign.status.value}",
            )
            continue
        for path in files:
            rel = path.relative_to(folder).as_posix()
            url = inbox_url(folder.name, rel)
            if compliance.classify_source_host(url) is not SourceHost.INBOX:  # pragma: no cover - by construction
                continue
            row, created = register_source(session, campaign, url)
            if created:
                created_rows.append(row)
    return created_rows
