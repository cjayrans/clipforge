"""Purge source media (ARCHITECTURE §4.1, PRD F2): delete a source's file 7 days after all its candidates are terminal.

Scheduled daily at 03:30 America/New_York by the worker (Phase 1 D / Phase 2): `purge_sources(session, settings)`.

A candidate is done when its own state is terminal, or when it has clips and every clip is terminal (the candidate row
can stay RENDERED while its clip moves on). Terminal states: QA_FAILED, REJECTED, PUBLISHED, EXPIRED. The 7 days
(`retention.source_days_after_terminal`) run from the latest terminal `events` row of those candidates and clips.

Sources with no candidates are purged only when they can never produce one (FAILED, or RANKED with nothing kept),
7 days after their last source event. Sources still in the pipeline are never touched.

Only files inside `media_dir` are deleted. The row keeps its sha256 (and the transcript is kept); status becomes
PURGED with `purged_at` set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CandidateRow, ClipRow, EventRow, SourceRow, utcnow
from clipforge.ingest.models import set_source_status
from clipforge.schemas import ClipState, SourceStatus

log = logging.getLogger(__name__)

TERMINAL: frozenset[ClipState] = frozenset(
    {ClipState.QA_FAILED, ClipState.REJECTED, ClipState.PUBLISHED, ClipState.EXPIRED}
)
NO_CANDIDATE_TERMINAL: frozenset[SourceStatus] = frozenset({SourceStatus.FAILED, SourceStatus.RANKED})


def _latest_event(
    session: Session, entity: str, ids: list[int], states: frozenset[ClipState] | None
) -> datetime | None:
    if not ids:
        return None
    q = select(func.max(EventRow.at)).where(EventRow.entity == entity, EventRow.entity_id.in_([str(i) for i in ids]))
    if states is not None:
        q = q.where(EventRow.to_state.in_([s.value for s in states]))
    return session.scalar(q)


def done_since(session: Session, source: SourceRow) -> datetime | None:
    """When the source's last candidate became terminal, or None if some candidate is still in play."""
    candidates = list(session.scalars(select(CandidateRow).where(CandidateRow.source_id == source.id)))
    if not candidates:
        if source.status not in NO_CANDIDATE_TERMINAL:
            return None
        return _latest_event(session, "source", [source.id], None) or source.created_at

    fallback: list[datetime] = []
    done_candidate_ids: list[int] = []
    clip_ids: list[int] = []
    for cand in candidates:
        clips = list(session.scalars(select(ClipRow).where(ClipRow.candidate_id == cand.id)))
        if cand.state in TERMINAL:
            done_candidate_ids.append(cand.id)
            fallback.append(cand.created_at)
        elif clips and all(c.state in TERMINAL for c in clips):
            clip_ids.extend(c.id for c in clips)
            fallback.extend(c.rendered_at for c in clips)
        else:
            return None
    stamps = [
        t
        for t in (
            _latest_event(session, "candidate", done_candidate_ids, TERMINAL),
            _latest_event(session, "clip", clip_ids, TERMINAL),
        )
        if t is not None
    ]
    return max(stamps) if stamps else max(fallback)


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def purge_sources(session: Session, settings: Settings, *, now: datetime | None = None) -> list[int]:
    """Delete eligible source files; return the purged source ids."""
    now = now or utcnow()
    keep = timedelta(days=settings.app.retention.source_days_after_terminal)
    purged: list[int] = []
    rows = session.scalars(
        select(SourceRow).where(SourceRow.status != SourceStatus.PURGED, SourceRow.local_path.is_not(None))
    ).all()
    for source in rows:
        since = done_since(session, source)
        if since is None or now - since < keep:
            continue
        path = Path(source.local_path or "")
        if not _inside(path, settings.media_dir):
            log.error("refusing to purge source %s: %s is outside the media dir", source.id, path)
            continue
        path.unlink(missing_ok=True)
        old_path = source.local_path
        source.local_path = None
        source.purged_at = now
        set_source_status(session, source, SourceStatus.PURGED, detail=f"deleted {old_path}")
        purged.append(source.id)
        log.info("purged source %s (%s)", source.id, old_path)
    return purged
