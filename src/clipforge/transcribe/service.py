"""Transcribe one downloaded source: extract audio -> chunk -> Groq -> merge -> `schemas.Transcript`.

Entry point for the pipeline (Phase 2): `transcribe_source(session, settings, source) -> schemas.Transcript`.

On success the transcript JSON is written to `media_dir/transcripts/<source_id>.json`, a `transcripts` row records
provider, model and cost, and the source becomes TRANSCRIBED (events row). Temporary audio is deleted.

Errors: `costs.SpendCapReached` propagates untouched (the pipeline pauses). Any other failure raises
`TranscriberError` after writing an operator alert; the source stays DOWNLOADED so the next night retries it. Commit
the session after catching it.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import SourceRow, TranscriptRow
from clipforge.ingest import alerts, media
from clipforge.ingest.models import set_source_status
from clipforge.schemas import SourceStatus, Transcript
from clipforge.transcribe import audio
from clipforge.transcribe.chunker import ChunkResult, merge_chunks, plan_chunks
from clipforge.transcribe.groq_client import ChunkOutput, GroqTranscriber, TranscriberError

log = logging.getLogger(__name__)


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


def _run_backend(
    backend: Transcriber, chunks: list[tuple[ChunkResult, Path]]
) -> tuple[list[ChunkResult], str | None, float]:
    results: list[ChunkResult] = []
    language: str | None = None
    cost = 0.0
    for res, path in chunks:
        out = backend.transcribe_chunk(path, res.plan.duration_s)
        results.append(ChunkResult(res.plan, out.words, out.segments))
        language = language or out.language
        cost += out.cost_usd
    return results, language, cost


def transcribe_source(
    session: Session,
    settings: Settings,
    source: SourceRow,
    *,
    transcriber: Transcriber | None = None,
    runner: media.Runner = media.run,
) -> Transcript:
    """Transcribe a DOWNLOADED source; see the module docstring."""
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

        backend = transcriber or GroqTranscriber(session, settings)
        results, language, cost = _run_backend(backend, chunks)
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
    set_source_status(
        session,
        source,
        SourceStatus.TRANSCRIBED,
        detail=f"{transcript.provider} {len(transcript.words)} words ${cost:.4f}",
    )
    return transcript
