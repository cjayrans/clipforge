"""Groq Whisper client (ARCHITECTURE §4.2, PRD F3). One class per external service, per CLAUDE.md.

Facts verified 2026-11-22 against https://console.groq.com/docs/speech-to-text, https://console.groq.com/docs/api-reference,
https://console.groq.com/docs/rate-limits and https://console.groq.com/docs/errors, plus the installed groq SDK 1.7.0:
- `POST https://api.groq.com/openai/v1/audio/transcriptions` (multipart), `Authorization: Bearer <key>`.
- Word timestamps need `response_format="verbose_json"` with `timestamp_granularities[]` = `word`, `segment`. (A code
  comment on the docs page says "json"; the parameter table and API reference say `verbose_json`. The SDK sends the
  field as `timestamp_granularities[]`; tests assert both.)
- Response words carry only `word`, `start`, `end` (no probability, so `Word.prob` is None); segments carry `start`,
  `end`, `text` (plus `avg_logprob`, `no_speech_prob`, ...). Top-level `language`/`duration` are not documented, so they
  are optional here.
- Max file 25 MB (free tier), 100 MB (dev tier); billed minimum 10 s per request; `whisper-large-v3-turbo` costs
  $0.04 per audio hour (price lives in config `prices`). 429 responses carry `retry-after` (seconds). No charge for
  server errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import groq
from sqlalchemy.orm import Session
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt, wait_exponential
from tenacity.wait import wait_base

from clipforge import costs
from clipforge.config import Settings
from clipforge.schemas import Segment, Word

log = logging.getLogger(__name__)

SERVICE = "groq"
MAX_ATTEMPTS = 3
MIN_BILLED_S = 10.0
REQUEST_TIMEOUT_S = 600.0  # a ~94 min chunk uploads ~22 MB
MAX_RETRY_AFTER_S = 120.0
DEFAULT_LANGUAGE = "en"
RETRY_WAIT: wait_base = wait_exponential(multiplier=2, min=2, max=60)


class TranscriberError(RuntimeError):
    """Transcription failed (after retries) or returned something unusable."""


@dataclass
class ChunkOutput:
    """One transcribed audio file; timestamps relative to the file start."""

    words: list[Word] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    cost_usd: float = 0.0


def _retryable(exc: BaseException) -> bool:
    # APITimeoutError subclasses APIConnectionError. 4xx other than 429 (bad key, bad file) are not retried.
    return isinstance(exc, groq.APIConnectionError | groq.RateLimitError | groq.InternalServerError)


class _RetryAfterOrExponential(wait_base):
    """Honour a 429's `retry-after` header (capped), else exponential backoff."""

    def __call__(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, groq.RateLimitError):
            try:
                return min(float(exc.response.headers.get("retry-after", "")), MAX_RETRY_AFTER_S)
            except ValueError:
                pass
        return float(RETRY_WAIT(retry_state))


def parse_verbose_json(data: dict[str, Any]) -> ChunkOutput:
    """Pure: Groq `verbose_json` -> words and segments. Word timestamps are required."""
    raw_words = data.get("words")
    if not isinstance(raw_words, list):
        raise TranscriberError("Groq response has no word timestamps (expected verbose_json with words)")
    words: list[Word] = []
    for w in raw_words:
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        start = max(0.0, float(w["start"]))
        words.append(Word(text=text, start_s=start, end_s=max(start, float(w["end"])), prob=None))
    segments: list[Segment] = []
    for s in data.get("segments") or []:
        text = str(s.get("text", "")).strip()
        start = max(0.0, float(s["start"]))
        segments.append(Segment(text=text, start_s=start, end_s=max(start, float(s["end"]))))
    language = data.get("language")
    return ChunkOutput(words=words, segments=segments, language=str(language)[:16] if language else None)


class GroqTranscriber:
    """Transcribe audio files with Groq Whisper; records cost per request and checks the spend cap first."""

    provider = "groq"

    def __init__(
        self,
        session: Session,
        settings: Settings,
        *,
        client: groq.Groq | None = None,
        language: str | None = DEFAULT_LANGUAGE,
    ) -> None:
        self.session = session
        self.settings = settings
        self.model = settings.app.models.transcribe
        self.language = language
        if client is None:
            key = settings.secrets.groq_api_key
            if key is None or not key.get_secret_value():
                raise TranscriberError("GROQ_API_KEY is not set")
            client = groq.Groq(api_key=key.get_secret_value(), max_retries=0, timeout=REQUEST_TIMEOUT_S)
        self.client = client

    def _create(self, path: Path) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.language:
            kwargs["language"] = self.language
        with open(path, "rb") as fh:
            resp = self.client.audio.transcriptions.create(
                model=self.model,
                file=(path.name, fh),
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
                temperature=0.0,
                **kwargs,
            )
        return resp.model_dump()

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
        """Transcribe one file (< 25 MB). Raises SpendCapReached before calling when the cap is hit."""
        costs.guard(self.session, self.settings.app)
        retrying = Retrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=_RetryAfterOrExponential(),
            retry=retry_if_exception(_retryable),
            reraise=True,
        )
        try:
            for attempt in retrying:
                with attempt:
                    data = self._create(path)
        except groq.APIError as exc:
            # Never include request details: the SDK error text has no key, but keep it short anyway.
            raise TranscriberError(f"Groq transcription failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
        out = parse_verbose_json(data)
        billed = max(MIN_BILLED_S, duration_s)
        row = costs.record(self.session, self.settings.app, SERVICE, self.model, billed, "audio_seconds")
        out.cost_usd = row.usd
        log.info("groq transcribed %s (%.0f s billed, $%.4f)", path.name, billed, row.usd)
        return out
