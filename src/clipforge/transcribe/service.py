"""Transcribe one downloaded source: extract audio -> chunk -> Groq -> merge -> `schemas.Transcript`.

Entry point for the pipeline (Phase 2): `transcribe_source(session, settings, source) -> schemas.Transcript`.

On success the transcript JSON is written to `media_dir/transcripts/<source_id>.json`, a `transcripts` row records
provider, model and cost, and the source becomes TRANSCRIBED (events row). Temporary audio is deleted.

No-speech guard (PRD F17): under 20 words per minute the transcript is still stored, but the source becomes FAILED
with `fail_reason="no_speech"` and the operator is alerted. Rank only sources whose status is TRANSCRIBED.

Fallback (PRD F3): if Groq fails twice on a chunk (each failure is already 3 retried attempts), or no Groq key is
set, the whole source is transcribed with faster-whisper on CPU (`transcribe.local`) and an operator alert notes it.

Errors: `costs.SpendCapReached` propagates untouched (the pipeline pauses). Any other failure raises
`TranscriberError` after writing an operator alert; the source stays DOWNLOADED so the next night retries it. Commit
the session after catching it.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import SourceRow, TranscriptRow
from clipforge.ingest import alerts, media
from clipforge.ingest.models import set_source_status
from clipforge.schemas import SourceStatus, Transcript
from clipforge.transcribe import audio
from clipforge.transcribe.chunker import ChunkResult, merge_chunks, plan_chunks
from clipforge.transcribe.groq_client import ChunkOutput, GroqTranscriber, TranscriberError
from clipforge.transcribe.guard import NO_SPEECH_REASON, NO_SPEECH_WPM, is_no_speech, words_per_minute
from clipforge.transcribe.local import LocalTranscriber, LocalUnavailable, local_fallback_enabled, local_model_name

log = logging.getLogger(__name__)

GROQ_FAILURES_BEFORE_FALLBACK = 2  # PRD F3: local fallback if Groq fails twice


class Transcriber(Protocol):
    """What `transcribe_source` needs from a backend (GroqTranscriber, LocalTranscriber or a test fake)."""

    provider: str
    model: str

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput: ...


def transcript_path(settings: Settings, source_id: int) -> Path:
    return settings.media_dir / "transcripts" / f"{source_id}.json"


def work_dir(settings: Settings, source_id: int) -> Path:
    return settings.media_dir / "work" / f"transcribe-{source_id}"


def load_transcript(path: Path | str) -> Transcript:
    return Transcript.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _transcribe_chunks(
    session: Session,
    settings: Settings,
    source: SourceRow,
    chunks: list[tuple[ChunkResult, Path]],
    transcriber: Transcriber | None,
    local: Transcriber | None,
) -> tuple[Transcriber, list[ChunkResult], str | None, float]:
    """Groq first; if one chunk fails `GROQ_FAILURES_BEFORE_FALLBACK` times, redo the whole source locally.

    Returns (backend used, chunk results, language, API cost). Cost includes Groq chunks that succeeded before a
    fallback (they were billed).
    """
    cost = 0.0
    last_error: TranscriberError | None = None
    primary: Transcriber | None
    try:
        primary = transcriber or GroqTranscriber(session, settings)
    except TranscriberError as exc:  # e.g. GROQ_API_KEY not set
        primary, last_error = None, exc

    if primary is not None:
        results: list[ChunkResult] = []
        language: str | None = None
        for res, path in chunks:
            for failure in range(1, GROQ_FAILURES_BEFORE_FALLBACK + 1):
                try:
                    out = primary.transcribe_chunk(path, res.plan.duration_s)
                    break
                except TranscriberError as exc:
                    last_error = exc
                    log.warning(
                        "source %s chunk %s: %s failure %d: %s",
                        source.id,
                        res.plan.index,
                        primary.provider,
                        failure,
                        exc,
                    )
            else:
                break  # this chunk failed twice: fall back
            results.append(ChunkResult(res.plan, out.words, out.segments))
            language = language or out.language
            cost += out.cost_usd
        else:
            return primary, results, language, cost

    assert last_error is not None
    if local is None:
        raise last_error
    alerts.alert_operator(
        session, alerts.TRANSCRIBE_FALLBACK, source.id, f"Groq failed ({last_error}); transcribing locally (slow)"
    )
    try:
        local_results: list[ChunkResult] = []
        local_language: str | None = None
        for res, path in chunks:
            out = local.transcribe_chunk(path, res.plan.duration_s)
            local_results.append(ChunkResult(res.plan, out.words, out.segments))
            local_language = local_language or out.language
            cost += out.cost_usd
    except LocalUnavailable as exc:
        raise TranscriberError(f"{last_error}; local fallback unavailable: {exc}") from exc
    return local, local_results, local_language, cost


def transcribe_source(
    session: Session,
    settings: Settings,
    source: SourceRow,
    *,
    transcriber: Transcriber | None = None,
    local: Transcriber | None | Literal["auto"] = "auto",
    runner: media.Runner = media.run,
) -> Transcript:
    """Transcribe a DOWNLOADED source; see the module docstring.

    `transcriber` defaults to Groq. `local` is the fallback: "auto" builds a faster-whisper LocalTranscriber when
    `local_fallback_enabled(settings)`; None disables the fallback.
    """
    if local == "auto":
        local = LocalTranscriber(local_model_name(settings)) if local_fallback_enabled(settings) else None
    if source.status is not SourceStatus.DOWNLOADED:
        raise TranscriberError(f"source {source.id} is {source.status.value}, not downloaded")
    if not source.local_path or not Path(source.local_path).is_file():
        raise TranscriberError(f"source {source.id} has no media file on disk")

    work = work_dir(settings, source.id)
    try:
        audio_path = audio.extract_audio(Path(source.local_path), work / "audio.mp3", runner)
        duration = media.probe(audio_path, runner).duration_s
        plans = plan_chunks(duration)
        paths = audio.cut_chunks(audio_path, plans, work / "chunks", runner)
        chunks = [(ChunkResult(p), path) for p, path in zip(plans, paths, strict=True)]

        backend, results, language, cost = _transcribe_chunks(session, settings, source, chunks, transcriber, local)
    except media.MediaToolError as exc:
        alerts.alert_operator(session, alerts.TRANSCRIBE_FAILED, source.id, f"audio extraction failed: {exc}")
        raise TranscriberError(str(exc)) from exc
    except TranscriberError as exc:
        alerts.alert_operator(session, alerts.TRANSCRIBE_FAILED, source.id, f"transcription failed: {exc}")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)

    words, segments = merge_chunks(results)
    transcript = Transcript(
        source_id=source.id,
        provider="groq" if backend.provider == "groq" else "faster_whisper",
        model=backend.model,
        language=language,
        words=words,
        segments=segments,
        duration_s=duration,
    )
    return _store(session, settings, source, transcript, cost)


def _store(session: Session, settings: Settings, source: SourceRow, transcript: Transcript, cost: float) -> Transcript:
    path = transcript_path(settings, source.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(transcript.model_dump_json(indent=1), encoding="utf-8")
    tmp.replace(path)
    session.add(
        TranscriptRow(
            source_id=source.id,
            provider=transcript.provider,
            model=transcript.model,
            words_json_path=str(path),
            language=transcript.language,
            cost_usd=cost,
        )
    )
    session.flush()
    wpm = words_per_minute(transcript)
    if is_no_speech(transcript):
        # PRD F17: keep the (paid-for) transcript for the record, but never rank this source.
        set_source_status(
            session, source, SourceStatus.FAILED, fail_reason=NO_SPEECH_REASON, detail=f"{wpm:.1f} words/min"
        )
        alerts.alert_operator(
            session,
            alerts.NO_SPEECH,
            source.id,
            f"source {source.id} has {wpm:.1f} words/min (< {NO_SPEECH_WPM:.0f}); v1 needs narrated or commentary "
            "footage, so it will not be ranked",
        )
        return transcript
    set_source_status(
        session,
        source,
        SourceStatus.TRANSCRIBED,
        detail=f"{transcript.provider} {len(transcript.words)} words ({wpm:.0f}/min) ${cost:.4f}",
    )
    return transcript
