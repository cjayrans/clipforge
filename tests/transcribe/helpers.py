"""Fakes shared by the transcribe tests (no ffmpeg, no network)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from clipforge.config import Settings
from clipforge.schemas import Segment, SourceStatus, Word
from clipforge.transcribe.groq_client import ChunkOutput
from tests.support.factories import Factory


def fake_runner(audio_duration: float) -> Any:
    """Fake ffmpeg (writes the output file) and ffprobe (reports `audio_duration`)."""
    calls: list[list[str]] = []

    def run(args: Sequence[str]) -> str:
        calls.append(list(args))
        if args[0] == "ffmpeg":
            Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[-1]).write_bytes(b"mp3")
            return ""
        return json.dumps({"format": {"duration": str(audio_duration)}, "streams": [{"codec_type": "audio"}]})

    run.calls = calls  # type: ignore[attr-defined]
    return run


class FakeTranscriber:
    """Hears one word every 0.5 s of each chunk (chunk-relative timestamps), like a real backend."""

    provider = "groq"
    model = "whisper-large-v3-turbo"

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    def transcribe_chunk(self, path: Path, duration_s: float) -> ChunkOutput:
        self.calls.append((path.name, duration_s))
        n = int(duration_s / 0.5)
        words = [Word(text=f"w{i}", start_s=i * 0.5, end_s=i * 0.5 + 0.3) for i in range(n)]
        return ChunkOutput(
            words=words, segments=[Segment(text="seg", start_s=0.0, end_s=duration_s)], language="en", cost_usd=0.01
        )


def downloaded_source(factory: Factory, settings: Settings, duration: float = 90.0) -> Any:
    media_file = settings.media_dir / "sources" / "x.mp4"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"video")
    return factory.source(status=SourceStatus.DOWNLOADED, local_path=str(media_file), duration_s=duration)
