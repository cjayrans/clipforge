"""A4b: no-speech guard (PRD F17): under 20 words/min -> source FAILED no_speech + operator alert."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import EventRow, TranscriptRow
from clipforge.schemas import SourceStatus, Transcript, Word
from clipforge.transcribe import guard, service
from clipforge.transcribe.groq_client import ChunkOutput
from tests.support.factories import Factory
from tests.transcribe.helpers import downloaded_source, fake_runner

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture(name: str) -> Transcript:
    return service.load_transcript(FIXTURES / name)


def test_fixture_transcripts() -> None:
    sparse = _fixture("transcript_sparse.json")
    talk = _fixture("transcript_talk.json")
    assert guard.words_per_minute(sparse) == pytest.approx(10.7, abs=0.1)
    assert guard.is_no_speech(sparse)
    assert guard.words_per_minute(talk) > 100
    assert not guard.is_no_speech(talk)


def _transcript(n_words: int, duration_s: float) -> Transcript:
    words = [Word(text="w", start_s=i * 0.1, end_s=i * 0.1) for i in range(n_words)]
    return Transcript(source_id=1, provider="groq", model="m", words=words, segments=[], duration_s=duration_s)


@pytest.mark.parametrize(
    ("n_words", "duration_s", "no_speech"),
    [
        (20, 60.0, False),  # exactly 20 wpm passes
        (19, 60.0, True),
        (0, 3600.0, True),
        (0, 0.0, True),  # zero-length audio counts as no speech
        (5, 10.0, False),  # 30 wpm
    ],
)
def test_threshold(n_words: int, duration_s: float, no_speech: bool) -> None:
    assert guard.is_no_speech(_transcript(n_words, duration_s)) is no_speech


class SparseTranscriber:
    provider = "groq"
    model = "whisper-large-v3-turbo"

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
        sparse = _fixture("transcript_sparse.json")
        return ChunkOutput(words=sparse.words, segments=sparse.segments, language="en", cost_usd=0.001)


def test_sparse_source_failed_with_alert(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)
    t = service.transcribe_source(session, settings, src, transcriber=SparseTranscriber(), runner=fake_runner(90.0))
    assert src.status is SourceStatus.FAILED
    assert src.fail_reason == "no_speech"
    # The paid-for transcript is still stored.
    row = session.scalars(select(TranscriptRow)).one()
    assert service.load_transcript(row.words_json_path) == t
    alert = session.scalars(select(EventRow).where(EventRow.entity == "alert")).one()
    assert alert.to_state == "no_speech"
    assert "words/min" in alert.detail
    ev = session.scalars(select(EventRow).where(EventRow.entity == "source")).all()[-1]
    assert (ev.from_state, ev.to_state) == ("downloaded", "failed")
