"""A4: Groq client (request shape, retries, cost) and transcribe_source end to end with fakes."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from tenacity import wait_none

from clipforge import costs
from clipforge.config import Settings, load_settings
from clipforge.db import CostRow, EventRow, FlagRow, TranscriptRow, make_session_factory
from clipforge.schemas import SourceStatus
from clipforge.transcribe import chunker, groq_client, service
from clipforge.transcribe.groq_client import ChunkOutput, GroqTranscriber, TranscriberError
from tests.support.factories import Factory
from tests.transcribe.helpers import FakeTranscriber, downloaded_source, fake_runner

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
FAKE_KEY = "test-groq-key-not-a-real-one"

VERBOSE = {
    "text": "hello chat we are in",
    "language": "en",
    "duration": 4.2,
    "words": [
        {"word": " hello", "start": 0.1, "end": 0.4},
        {"word": "chat,", "start": 0.5, "end": 0.9},
        {"word": "", "start": 1.0, "end": 1.0},
        {"word": "we", "start": 1.2, "end": 1.3},
    ],
    "segments": [
        {"id": 0, "seek": 0, "start": 0.0, "end": 1.0, "text": " hello chat,", "no_speech_prob": 0.01},
        {"id": 1, "seek": 0, "start": 1.1, "end": 1.4, "text": " we", "no_speech_prob": 0.02},
    ],
    "x_groq": {"id": "req_test"},
}


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(groq_client, "RETRY_WAIT", wait_none())
    monkeypatch.setattr(groq_client, "MAX_RETRY_AFTER_S", 0.0)


@pytest.fixture
def keyed_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    return load_settings()


@pytest.fixture
def chunk_file(tmp_path: Path) -> Path:
    p = tmp_path / "chunk-000.mp3"
    p.write_bytes(b"ID3fake-mp3")
    return p


# --- parsing ------------------------------------------------------------------------------------------


def test_parse_verbose_json() -> None:
    out = groq_client.parse_verbose_json(VERBOSE)
    assert [(w.text, w.start_s, w.end_s, w.prob) for w in out.words] == [
        ("hello", 0.1, 0.4, None),
        ("chat,", 0.5, 0.9, None),
        ("we", 1.2, 1.3, None),
    ]
    assert [s.text for s in out.segments] == ["hello chat,", "we"]
    assert out.language == "en"


def test_parse_requires_word_timestamps() -> None:
    # What a plain `json` response looks like: text only. We must refuse it, not silently lose timestamps.
    with pytest.raises(TranscriberError, match="word timestamps"):
        groq_client.parse_verbose_json({"text": "hello", "x_groq": {"id": "x"}})


# --- client -------------------------------------------------------------------------------------------


def test_missing_key(session: Session, settings: Settings) -> None:
    with pytest.raises(TranscriberError, match="GROQ_API_KEY is not set"):
        GroqTranscriber(session, settings)


@respx.mock
def test_request_shape_and_cost(session: Session, keyed_settings: Settings, chunk_file: Path) -> None:
    route = respx.post(GROQ_URL).respond(200, json=VERBOSE)
    out = GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 3600.0)
    assert [w.text for w in out.words] == ["hello", "chat,", "we"]

    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert req.headers["content-type"].startswith("multipart/form-data")
    body = req.content.decode("latin-1")

    def field(name: str) -> list[str]:
        parts = body.split(f'name="{name}"')[1:]
        return [p.split("\r\n\r\n", 1)[1].split("\r\n", 1)[0] for p in parts]

    assert field("model") == ["whisper-large-v3-turbo"]
    assert field("response_format") == ["verbose_json"]
    assert sorted(field("timestamp_granularities[]")) == ["segment", "word"]
    assert field("language") == ["en"]
    assert field("temperature") == ["0.0"]
    assert 'filename="chunk-000.mp3"' in body

    # $0.04 per audio hour (config prices) for one hour.
    (cost,) = session.scalars(select(CostRow)).all()
    assert (cost.service, cost.model, cost.unit_kind, cost.units) == (
        "groq",
        "whisper-large-v3-turbo",
        "audio_seconds",
        3600.0,
    )
    assert cost.usd == pytest.approx(0.04)
    assert out.cost_usd == pytest.approx(0.04)


@respx.mock
def test_minimum_billed_ten_seconds(session: Session, keyed_settings: Settings, chunk_file: Path) -> None:
    respx.post(GROQ_URL).respond(200, json=VERBOSE)
    GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 3.0)
    assert session.scalars(select(CostRow.units)).one() == 10.0


@respx.mock
def test_retries_then_succeeds(session: Session, keyed_settings: Settings, chunk_file: Path) -> None:
    route = respx.post(GROQ_URL).mock(
        side_effect=[
            httpx.Response(503, json={"error": {"message": "over capacity", "type": "server_error"}}),
            httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=VERBOSE),
        ]
    )
    GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 60.0)
    assert route.call_count == 3
    assert len(session.scalars(select(CostRow)).all()) == 1  # only the success is billed


@respx.mock
def test_gives_up_after_three_attempts(session: Session, keyed_settings: Settings, chunk_file: Path) -> None:
    route = respx.post(GROQ_URL).respond(429, json={"error": {"message": "rate limited"}})
    with pytest.raises(TranscriberError, match="RateLimitError"):
        GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 60.0)
    assert route.call_count == 3
    assert session.scalars(select(CostRow)).all() == []


@respx.mock
def test_connection_errors_retried(session: Session, keyed_settings: Settings, chunk_file: Path) -> None:
    route = respx.post(GROQ_URL).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(200, json=VERBOSE)])
    GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 60.0)
    assert route.call_count == 2


@respx.mock
def test_auth_error_not_retried_and_key_not_leaked(
    session: Session, keyed_settings: Settings, chunk_file: Path
) -> None:
    route = respx.post(GROQ_URL).respond(
        401, json={"error": {"message": "Invalid API Key", "type": "invalid_request_error"}}
    )
    with pytest.raises(TranscriberError) as exc:
        GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 60.0)
    assert route.call_count == 1
    assert FAKE_KEY not in str(exc.value)


@respx.mock(assert_all_called=False)
def test_spend_cap_checked_before_calling(session: Session, keyed_settings: Settings, chunk_file: Path) -> None:
    route = respx.post(GROQ_URL).respond(200, json=VERBOSE)
    costs.pause(session, "spend cap")
    with pytest.raises(costs.SpendCapReached):
        GroqTranscriber(session, keyed_settings).transcribe_chunk(chunk_file, 60.0)
    assert not route.called
    assert session.get(FlagRow, costs.PAUSE_FLAG) is not None


# --- transcribe_source with fakes ---------------------------------------------------------------------


def test_transcribe_source_single_chunk(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)
    fake = FakeTranscriber()
    runner = fake_runner(90.0)
    t = service.transcribe_source(session, settings, src, transcriber=fake, runner=runner)
    assert fake.calls == [("audio.mp3", 90.0)]
    assert t.provider == "groq"
    assert t.duration_s == 90.0
    assert len(t.words) == 180
    assert src.status is SourceStatus.TRANSCRIBED
    row = session.scalars(select(TranscriptRow)).one()
    assert row.cost_usd == pytest.approx(0.01)
    assert service.load_transcript(row.words_json_path) == t
    assert not service.work_dir(settings, src.id).exists()  # temp audio cleaned
    ffmpeg = [c for c in runner.calls if c[0] == "ffmpeg"]
    assert "-map" in ffmpeg[0]


def test_transcribe_source_multi_chunk(
    session: Session, settings: Settings, factory: Factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "plan_chunks", lambda d: chunker.plan_chunks(d, max_bytes=200_000))  # 45 s chunks
    src = downloaded_source(factory, settings, 200.0)
    fake = FakeTranscriber()
    t = service.transcribe_source(session, settings, src, transcriber=fake, runner=fake_runner(200.0))
    assert len(fake.calls) == 5
    assert all(name.startswith("chunk-") for name, _ in fake.calls)
    starts = [w.start_s for w in t.words]
    assert starts == sorted(starts)
    assert len(starts) == len(set(starts))  # overlap de-duplicated
    assert t.words[-1].start_s > 195
    assert session.scalars(select(TranscriptRow.cost_usd)).one() == pytest.approx(0.05)


def test_not_downloaded_refused(session: Session, settings: Settings, factory: Factory) -> None:
    src = factory.source()
    with pytest.raises(TranscriberError, match="not downloaded"):
        service.transcribe_source(session, settings, src, transcriber=FakeTranscriber(), runner=fake_runner(90))


def test_backend_failure_alerts_and_keeps_status(session: Session, settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, settings)

    class Broken(FakeTranscriber):
        def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
            raise TranscriberError("Groq transcription failed: InternalServerError")

    with pytest.raises(TranscriberError):
        service.transcribe_source(session, settings, src, transcriber=Broken(), runner=fake_runner(90), local=None)
    assert src.status is SourceStatus.DOWNLOADED
    alert = session.scalars(select(EventRow).where(EventRow.entity == "alert")).one()
    assert alert.to_state == "transcribe_failed"
    assert not service.work_dir(settings, src.id).exists()


def test_spend_cap_propagates(session: Session, keyed_settings: Settings, factory: Factory) -> None:
    src = downloaded_source(factory, keyed_settings)
    costs.pause(session, "spend cap")
    with pytest.raises(costs.SpendCapReached):
        service.transcribe_source(session, keyed_settings, src, runner=fake_runner(90), local=None)
    assert src.status is SourceStatus.DOWNLOADED


# --- Phase 2: cost ledger and configured language -----------------------------------------------------------


@respx.mock
def test_ledger_cost_rows_survive_caller_rollback(
    engine: Engine, session: Session, keyed_settings: Settings, chunk_file: Path
) -> None:
    """COMPLIANCE #12: with a ledger, the Groq cost row commits on its own, so a caller rollback can't erase it."""
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    ledger = make_session_factory(engine)
    t = GroqTranscriber(session, keyed_settings, ledger=ledger)
    t.transcribe_chunk(chunk_file, 60.0)
    session.rollback()
    with ledger() as s:
        rows = s.scalars(select(CostRow)).all()
    assert [(r.service, r.unit_kind, r.units) for r in rows] == [("groq", "audio_seconds", 60.0)]


@respx.mock(assert_all_called=False)
def test_ledger_guard_blocks_before_request(
    engine: Engine, session: Session, keyed_settings: Settings, chunk_file: Path
) -> None:
    route = respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    ledger = make_session_factory(engine)
    with ledger.begin() as s:
        costs.pause(s, "test")
    with pytest.raises(costs.SpendCapReached):
        GroqTranscriber(session, keyed_settings, ledger=ledger).transcribe_chunk(chunk_file, 60.0)
    assert not route.called


@respx.mock
def test_language_comes_from_config(
    session: Session, keyed_settings: Settings, chunk_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=VERBOSE))
    app = keyed_settings.app.model_copy(
        update={"transcribe": keyed_settings.app.transcribe.model_copy(update={"language": "de"})}
    )
    s2 = Settings(app=app, secrets=keyed_settings.secrets, env=keyed_settings.env)
    GroqTranscriber(session, s2).transcribe_chunk(chunk_file, 60.0)
    body = route.calls.last.request.content
    assert b'name="language"\r\n\r\nde' in body
    # An explicit argument still wins (None = let the model detect the language).
    GroqTranscriber(session, s2, language=None).transcribe_chunk(chunk_file, 60.0)
    assert b'name="language"' not in route.calls.last.request.content


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"text": "hello", "x_groq": {"id": "x"}},  # no word timestamps
        {"text": "hello", "words": [{"word": "hi", "end": 1.0}], "x_groq": {"id": "x"}},  # a word without start
    ],
)
def test_billed_response_that_fails_to_parse_is_still_recorded(
    engine: Engine, session: Session, keyed_settings: Settings, chunk_file: Path, body: dict[str, object]
) -> None:
    """COMPLIANCE #12 (Phase 2 review): a 200 is billed even when its body is unusable; the cost row must exist so the
    retry that follows can't spend past the cap unnoticed."""
    respx.post(GROQ_URL).mock(return_value=httpx.Response(200, json=body))
    ledger = make_session_factory(engine)
    with pytest.raises(TranscriberError):
        GroqTranscriber(session, keyed_settings, ledger=ledger).transcribe_chunk(chunk_file, 60.0)
    session.rollback()
    with ledger() as s:
        assert [r.units for r in s.scalars(select(CostRow))] == [60.0]
