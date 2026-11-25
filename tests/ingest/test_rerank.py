"""`cf source rerank`: only a ranked source of an active campaign goes back to TRANSCRIBED (audited); history stays."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import ClipRow, EventRow, TranscriptRow, transition
from clipforge.ingest.registry import RegistryError, rerank_source
from clipforge.pipeline import USED_CLIP_STATES, used_windows
from clipforge.rank.moment_score import overlaps_any
from clipforge.schemas import ClipState, SourceStatus
from tests.support.factories import Factory


def _ranked(factory: Factory, *, status: str = "active", with_transcript: bool = True):
    src = factory.source(factory.campaign(status=status), status=SourceStatus.RANKED)
    if with_transcript:
        factory._add(TranscriptRow(source_id=src.id, provider="groq", model="m", words_json_path="/x.json"))
    return src


def _rendered(factory: Factory, session: Session, src, **kw):
    cand = factory.candidate(src, **kw)
    transition(session, cand, ClipState.RENDERED)
    return cand


def _advance(session: Session, clip: ClipRow, *states: ClipState) -> None:
    for state in states:
        transition(session, clip, state)


def test_rerank_moves_a_ranked_source_to_transcribed_with_an_audit_row(factory: Factory, session: Session) -> None:
    src = _ranked(factory)
    rerank_source(session, None, src.id)
    assert src.status is SourceStatus.TRANSCRIBED
    ev = session.scalars(select(EventRow).where(EventRow.entity == "source").order_by(EventRow.id.desc())).first()
    assert ev is not None
    assert (ev.from_state, ev.to_state, ev.actor) == ("ranked", "transcribed", "operator")
    assert "re-rank" in ev.detail


@pytest.mark.parametrize(
    "status", [SourceStatus.REGISTERED, SourceStatus.DOWNLOADED, SourceStatus.TRANSCRIBED, SourceStatus.FAILED]
)
def test_rerank_refuses_a_source_that_is_not_ranked(factory: Factory, session: Session, status: SourceStatus) -> None:
    src = factory.source(factory.campaign(), status=status)
    with pytest.raises(RegistryError, match="only ranked sources"):
        rerank_source(session, None, src.id)
    assert src.status is status


@pytest.mark.parametrize("campaign_status", ["drained", "ended"])
def test_rerank_refuses_an_inactive_campaign(factory: Factory, session: Session, campaign_status: str) -> None:
    src = _ranked(factory, status=campaign_status)
    with pytest.raises(RegistryError, match="only active campaigns"):
        rerank_source(session, None, src.id)
    assert src.status is SourceStatus.RANKED


def test_rerank_refuses_unknown_source_and_missing_transcript(factory: Factory, session: Session) -> None:
    with pytest.raises(RegistryError, match="no source"):
        rerank_source(session, None, 999)
    src = _ranked(factory, with_transcript=False)
    with pytest.raises(RegistryError, match="no transcript"):
        rerank_source(session, None, src.id)
    assert src.status is SourceStatus.RANKED


def test_rerank_keeps_history_and_never_touches_clips(factory: Factory, session: Session) -> None:
    src = _ranked(factory)
    rendered = _rendered(factory, session, src, start_s=0.0, end_s=30.0)
    clip = factory.clip(rendered)
    _advance(session, clip, ClipState.PENDING_REVIEW, ClipState.APPROVED)
    unrendered = factory.candidate(src, start_s=100.0, end_s=130.0)
    session.flush()

    rerank_source(session, None, src.id)

    assert clip.state is ClipState.APPROVED  # existing clips are never altered
    assert rendered.state is ClipState.RENDERED
    assert unrendered.state is ClipState.EXPIRED  # never rendered: expired so it is not rendered after the re-rank
    ev = session.scalars(
        select(EventRow).where(EventRow.entity == "candidate", EventRow.entity_id == str(unrendered.id))
    ).one()
    assert (ev.from_state, ev.to_state, ev.actor) == ("candidate", "expired", "operator")


def test_used_windows_are_only_approved_scheduled_or_published(factory: Factory, session: Session) -> None:
    src = _ranked(factory)
    paths = {
        ClipState.PENDING_REVIEW: (ClipState.PENDING_REVIEW,),
        ClipState.APPROVED: (ClipState.PENDING_REVIEW, ClipState.APPROVED),
        ClipState.SCHEDULED: (ClipState.PENDING_REVIEW, ClipState.APPROVED, ClipState.SCHEDULED),
        ClipState.PUBLISHED: (ClipState.PENDING_REVIEW, ClipState.APPROVED, ClipState.SCHEDULED, ClipState.PUBLISHED),
        ClipState.REJECTED: (ClipState.PENDING_REVIEW, ClipState.REJECTED),
    }
    expected = []
    for i, (state, path) in enumerate(paths.items()):
        cand = _rendered(factory, session, src, start_s=i * 100.0, end_s=i * 100.0 + 30.0)
        _advance(session, factory.clip(cand), *path)
        if state in USED_CLIP_STATES:
            expected.append((i * 100.0, i * 100.0 + 30.0))
    other = _ranked(factory)  # another source's clips never block this one
    _advance(session, factory.clip(_rendered(factory, session, other)), ClipState.PENDING_REVIEW)
    assert sorted(used_windows(session, src.id)) == sorted(expected)
    assert len(expected) == 3


def test_overlaps_any() -> None:
    used = [(100.0, 130.0)]
    assert overlaps_any((90.0, 101.0), used)
    assert overlaps_any((110.0, 120.0), used)
    assert overlaps_any((129.0, 160.0), used)
    assert not overlaps_any((70.0, 100.0), used)  # touching ends share no footage
    assert not overlaps_any((130.0, 160.0), used)
    assert not overlaps_any((0.0, 10.0), [])
