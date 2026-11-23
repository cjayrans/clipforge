"""A6: purge source media 7 days after all its candidates are terminal."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import EventRow, SourceRow, transition, utcnow
from clipforge.ingest.purge import purge_sources
from clipforge.schemas import ClipState, SourceStatus
from tests.support.factories import Factory

_n = itertools.count(1)


def _media_source(
    factory: Factory, settings: Settings, status: SourceStatus = SourceStatus.RANKED, **kw: object
) -> SourceRow:
    path = settings.media_dir / "sources" / "1" / f"{next(_n)}.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"footage")
    return factory.source(status=status, local_path=str(path), sha256="a" * 64, **kw)


def _later(days: float) -> datetime:
    return utcnow() + timedelta(days=days)


def test_all_candidates_terminal_purged_after_seven_days(
    session: Session, settings: Settings, factory: Factory
) -> None:
    src = _media_source(factory, settings)
    expired = factory.candidate(src)
    transition(session, expired, ClipState.EXPIRED)
    rendered = factory.candidate(src)
    transition(session, rendered, ClipState.RENDERED)  # candidate row stays RENDERED; its clip carries on
    clip = factory.clip(rendered)
    transition(session, clip, ClipState.PENDING_REVIEW)
    transition(session, clip, ClipState.REJECTED, actor="operator")
    path = Path(src.local_path or "")

    assert purge_sources(session, settings, now=_later(6.9)) == []
    assert path.exists()

    assert purge_sources(session, settings, now=_later(7.1)) == [src.id]
    assert not path.exists()
    assert src.status is SourceStatus.PURGED
    assert src.purged_at is not None
    assert src.local_path is None
    assert src.sha256 == "a" * 64  # hash kept
    ev = session.scalars(select(EventRow).where(EventRow.entity == "source")).all()[-1]
    assert ev.to_state == "purged"
    assert str(path) in ev.detail
    # Idempotent.
    assert purge_sources(session, settings, now=_later(30)) == []


def test_pending_candidate_blocks_purge(session: Session, settings: Settings, factory: Factory) -> None:
    src = _media_source(factory, settings)
    done = factory.candidate(src)
    transition(session, done, ClipState.EXPIRED)
    factory.candidate(src)  # still CANDIDATE
    assert purge_sources(session, settings, now=_later(60)) == []


@pytest.mark.parametrize(
    "clip_state_path", [[ClipState.PENDING_REVIEW], [ClipState.PENDING_REVIEW, ClipState.APPROVED]]
)
def test_live_clip_blocks_purge(
    session: Session, settings: Settings, factory: Factory, clip_state_path: list[ClipState]
) -> None:
    src = _media_source(factory, settings)
    cand = factory.candidate(src)
    transition(session, cand, ClipState.RENDERED)
    clip = factory.clip(cand)
    for st in clip_state_path:
        transition(session, clip, st)
    assert purge_sources(session, settings, now=_later(60)) == []


def test_one_clip_terminal_other_not(session: Session, settings: Settings, factory: Factory) -> None:
    src = _media_source(factory, settings)
    cand = factory.candidate(src)
    transition(session, cand, ClipState.RENDERED)
    a = factory.clip(cand, account_set="gaming-1")
    factory.clip(cand, account_set="gaming-2")  # still RENDERED
    transition(session, a, ClipState.QA_FAILED)
    assert purge_sources(session, settings, now=_later(60)) == []


def test_failed_source_without_candidates(session: Session, settings: Settings, factory: Factory) -> None:
    src = _media_source(factory, settings, status=SourceStatus.FAILED, fail_reason="no_speech")
    assert purge_sources(session, settings, now=_later(7.1)) == [src.id]
    assert src.fail_reason == "no_speech"  # why it failed is kept


@pytest.mark.parametrize("status", [SourceStatus.DOWNLOADED, SourceStatus.TRANSCRIBED])
def test_in_pipeline_sources_untouched(
    session: Session, settings: Settings, factory: Factory, status: SourceStatus
) -> None:
    src = _media_source(factory, settings, status=status)
    assert purge_sources(session, settings, now=_later(365)) == []
    assert Path(src.local_path or "").exists()


def test_file_outside_media_dir_never_deleted(
    session: Session, settings: Settings, factory: Factory, tmp_path: Path
) -> None:
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"keep me")
    src = factory.source(status=SourceStatus.FAILED, fail_reason="x", local_path=str(outside))
    assert purge_sources(session, settings, now=_later(30)) == []
    assert outside.exists()
    assert src.status is SourceStatus.FAILED


def test_missing_file_still_marked_purged(session: Session, settings: Settings, factory: Factory) -> None:
    src = _media_source(factory, settings, status=SourceStatus.FAILED, fail_reason="x")
    Path(src.local_path or "").unlink()
    assert purge_sources(session, settings, now=_later(8)) == [src.id]
