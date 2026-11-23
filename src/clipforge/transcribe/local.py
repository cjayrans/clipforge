"""Local faster-whisper fallback (ARCHITECTURE §4.2, PRD F3): CPU, int8, slow; used only when Groq fails twice.

`faster_whisper` is installed on Linux only (Docker, CI): it is imported lazily, and on the Mac `LocalTranscriber`
raises `LocalUnavailable`, which `transcribe_source` treats as "no fallback".

faster-whisper facts (checked 2026-11-22 against https://github.com/SYSTRAN/faster-whisper README and
faster_whisper/transcribe.py on master):
- `WhisperModel(model_size_or_path, device="cpu", compute_type="int8", cpu_threads=0, download_root=None)`; size names
  include `tiny` and `large-v3-turbo` (downloaded from the Hugging Face Hub on first use).
- `model.transcribe(audio_path, language=..., word_timestamps=True, vad_filter=False, beam_size=5, temperature=...)`
  returns `(segments, info)`; `segments` is a lazy generator (transcription runs while iterating).
- `Segment` dataclass: `start`, `end`, `text`, `words` (None unless word_timestamps), ...; `Word` dataclass: `start`,
  `end`, `word`, `probability`; `TranscriptionInfo.language`, `.duration`.
- Audio is decoded with PyAV, so no system ffmpeg is needed for this step.

Feature flag: the PRD asks for the fallback "behind a flag": `transcribe.local_fallback` in config (default true), with
`transcribe.local_model` and `transcribe.language` (CR-A2, resolved in Phase 2).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from clipforge.config import Settings
from clipforge.schemas import Segment, Word
from clipforge.transcribe.groq_client import ChunkOutput, TranscriberError

log = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "large-v3-turbo"
DEFAULT_LANGUAGE = "en"


class LocalUnavailable(TranscriberError):
    """faster-whisper is not installed here (e.g. the Intel Mac)."""


def local_fallback_enabled(settings: Settings) -> bool:
    return settings.app.transcribe.local_fallback


def local_model_name(settings: Settings) -> str:
    return settings.app.transcribe.local_model


def output_from_faster_whisper(segments: Any, language: str | None) -> ChunkOutput:
    """Pure: faster-whisper segments (with words) -> ChunkOutput. Consumes the generator."""
    words: list[Word] = []
    segs: list[Segment] = []
    for seg in segments:
        start = max(0.0, float(seg.start))
        segs.append(Segment(text=str(seg.text).strip(), start_s=start, end_s=max(start, float(seg.end))))
        for w in seg.words or []:
            text = str(w.word).strip()
            if not text:
                continue
            ws = max(0.0, float(w.start))
            prob = None if w.probability is None else min(1.0, max(0.0, float(w.probability)))
            words.append(Word(text=text, start_s=ws, end_s=max(ws, float(w.end)), prob=prob))
    return ChunkOutput(words=words, segments=segs, language=language, cost_usd=0.0)


class LocalTranscriber:
    """faster-whisper on CPU (int8). No API cost; nothing is recorded in the cost ledger."""

    provider = "faster_whisper"

    def __init__(self, model: str = DEFAULT_LOCAL_MODEL, *, language: str | None = DEFAULT_LANGUAGE) -> None:
        self.model = model
        self.language = language
        self._whisper: Any = None

    def _load(self) -> Any:
        if self._whisper is None:
            try:
                from faster_whisper import WhisperModel  # type: ignore[import-not-found,unused-ignore]
            except ImportError as exc:
                raise LocalUnavailable("faster-whisper is not installed (Linux/Docker only)") from exc
            log.info("loading faster-whisper %s (cpu, int8)", self.model)
            self._whisper = WhisperModel(self.model, device="cpu", compute_type="int8")
        return self._whisper

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
        model = self._load()
        try:
            segments, info = model.transcribe(str(path), language=self.language, word_timestamps=True)
            return output_from_faster_whisper(segments, getattr(info, "language", None))
        except Exception as exc:
            raise TranscriberError(f"faster-whisper failed: {type(exc).__name__}: {exc}") from exc
