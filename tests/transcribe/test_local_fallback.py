"""A5: faster-whisper fallback when Groq fails twice (fakes), plus a slow real-model test on Linux."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CostRow, EventRow, TranscriptRow
from clipforge.schemas import SourceStatus
from clipforge.transcribe import chunker, local, service
from clipforge.transcribe.groq_client import ChunkOutput, TranscriberError
from clipforge.transcribe.local import LocalTranscriber, LocalUnavailable
from tests.support.factories import Factory
from tests.transcribe.helpers import FakeTranscriber, downloaded_source, fake_runner

FIXTURE_VIDEO = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_90s.mp4"


class FlakyGroq(FakeTranscriber):
    """Fails `fail_times` times on chunk index `fail_on` (by call order), succeeds otherwise."""

    def __init__(self, fail_on: int = 0, fail_times: int = 2) -> None:
        super().__init__()
        self.fail_on, self.fail_times, self.chunk_seen = fail_on, fail_times, -1
        self.failures = 0
        self._last: str | None = None

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
        if path.name != self._last:
            self.chunk_seen += 1
            self._last = path.name
        if self.chunk_seen == self.fail_on and self.failures < self.fail_times:
            self.failures += 1
            self.calls.append((path.name, duration_s))
            raise TranscriberError("Groq transcription failed: InternalServerError")
        out = super().transcribe_chunk(path, duration_s)
        return out


class FakeLocal(FakeTranscriber):
    provider = "faster_whisper"
    model = "large-v3-turbo"

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
        out = super().transcribe_chunk(path, duration_s)
        out.cost_usd = 0.0
        return out


def _alerts(session: Session) -> list[str | None]:
    return [e.to_state for e in session.scalars(select(EventRow).where(EventRow.entity == "alert"))]


def test_one_groq_failure_is_retried_not_fallen_back(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)
    groq, loc = FlakyGroq(fail_times=1), FakeLocal()
    t = service.transcribe_source(session, settings, src, transcriber=groq, local=loc, runner=fake_runner(90))
    assert t.provider == "groq"
    assert loc.calls == []
    assert len(groq.calls) == 2


def test_two_groq_failures_fall_back_to_local(
    session: Session, settings: Settings, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "plan_chunks", lambda d: chunker.plan_chunks(d, max_bytes=200_000))
    src = downloaded_source(factory, settings, 120.0)
    groq, loc = FlakyGroq(fail_on=1), FakeLocal()  # chunk 0 succeeds, chunk 1 fails twice
    t = service.transcribe_source(session, settings, src, transcriber=groq, local=loc, runner=fake_runner(120))
    assert t.provider == "faster_whisper"
    assert t.model == "large-v3-turbo"
    assert len(loc.calls) == 3  # the whole source is redone locally, for one consistent transcript
    assert src.status is SourceStatus.TRANSCRIBED
    assert _alerts(session) == ["transcribe_fallback"]
    # The Groq chunk that succeeded was billed and stays in the transcript cost.
    assert session.scalars(select(TranscriptRow.cost_usd)).one() == pytest.approx(0.01)


def test_no_groq_key_goes_straight_to_local(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)
    loc = FakeLocal()
    t = service.transcribe_source(session, settings, src, local=loc, runner=fake_runner(90))
    assert t.provider == "faster_whisper"
    assert "GROQ_API_KEY" in session.scalars(select(EventRow.detail).where(EventRow.entity == "alert")).one()
    assert session.scalars(select(CostRow)).all() == []


def test_fallback_disabled(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)
    with pytest.raises(TranscriberError, match="InternalServerError"):
        service.transcribe_source(session, settings, src, transcriber=FlakyGroq(), local=None, runner=fake_runner(90))
    assert src.status is SourceStatus.DOWNLOADED


def test_local_unavailable_reports_both(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)

    class Missing(FakeLocal):
        def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
            raise LocalUnavailable("faster-whisper is not installed (Linux/Docker only)")

    with pytest.raises(TranscriberError, match="local fallback unavailable") as exc:
        service.transcribe_source(
            session, settings, src, transcriber=FlakyGroq(), local=Missing(), runner=fake_runner(90)
        )
    assert "InternalServerError" in str(exc.value)
    assert _alerts(session) == ["transcribe_fallback", "transcribe_failed"]
    assert src.status is SourceStatus.DOWNLOADED


def test_flag_defaults(settings: Settings) -> None:
    # Until Phase 2 adds `transcribe:` to AppConfig (contract request), the fallback is on with large-v3-turbo.
    assert local.local_fallback_enabled(settings) is True
    assert local.local_model_name(settings) == "large-v3-turbo"


# --- mapping faster-whisper output --------------------------------------------------------------------


@dataclass
class FWWord:
    start: float
    end: float
    word: str
    probability: float | None


@dataclass
class FWSegment:
    start: float
    end: float
    text: str
    words: list[FWWord] | None


def test_output_from_faster_whisper() -> None:
    segs = iter(
        [
            FWSegment(0.0, 1.5, " Okay chat,", [FWWord(0.0, 0.4, " Okay", 0.91), FWWord(0.5, 0.9, " chat,", 1.2)]),
            FWSegment(1.6, 2.0, " go", [FWWord(1.6, 1.9, " ", 0.5), FWWord(1.6, 1.9, " go", None)]),
            FWSegment(2.1, 2.5, " (no words)", None),
        ]
    )
    out = local.output_from_faster_whisper(segs, "en")
    assert [(w.text, w.prob) for w in out.words] == [("Okay", 0.91), ("chat,", 1.0), ("go", None)]
    assert [s.text for s in out.segments] == ["Okay chat,", "go", "(no words)"]
    assert out.language == "en"
    assert out.cost_usd == 0.0


def test_local_transcriber_unavailable_without_package(tmp_path: Path) -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        with pytest.raises(LocalUnavailable):
            LocalTranscriber("tiny").transcribe_chunk(tmp_path / "a.mp3", 1.0)
    else:  # pragma: no cover - Linux
        pytest.skip("faster-whisper installed")


@pytest.mark.slow
@pytest.mark.needs_ffmpeg
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_tiny_model_on_fixture(tmp_path: Path) -> None:
    pytest.importorskip("faster_whisper")
    from clipforge.transcribe import audio

    mp3 = audio.extract_audio(FIXTURE_VIDEO, tmp_path / "a.mp3")
    out = LocalTranscriber("tiny").transcribe_chunk(mp3, 90.0)
    # The fixture is a sine tone: we only check the call works and timestamps are sane.
    for w in out.words:
        assert 0.0 <= w.start_s <= w.end_s <= 91.0
        assert w.prob is None or 0.0 <= w.prob <= 1.0
