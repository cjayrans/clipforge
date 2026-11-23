"""A3: audio extraction args, chunk planning (size + overlap) and overlap merge."""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clipforge.ingest import media
from clipforge.schemas import Segment, Transcript, Word
from clipforge.transcribe import audio, chunker
from clipforge.transcribe.chunker import ChunkPlan, ChunkResult, merge_chunks, plan_chunks

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BYTES_PER_S = chunker.AUDIO_BITRATE_BPS / 8

# --- planning -----------------------------------------------------------------------------------------


def test_one_hour_is_one_chunk() -> None:
    assert plan_chunks(3600) == [ChunkPlan(0, 0.0, 3600)]


def test_max_chunk_is_about_94_minutes() -> None:
    max_s = chunker.max_chunk_seconds()
    assert 90 * 60 < max_s < 96 * 60  # ARCHITECTURE: < 24 MB ≈ 95 min at 32 kbps
    assert max_s * BYTES_PER_S < 24 * 1024 * 1024


def test_ten_hours() -> None:
    plans = plan_chunks(10 * 3600)
    assert len(plans) == 7
    assert plans[0].start_s == 0
    assert plans[-1].end_s == 10 * 3600


def test_zero_duration() -> None:
    assert plan_chunks(0) == []


def test_limit_too_small() -> None:
    with pytest.raises(ValueError, match="too small"):
        plan_chunks(100, max_bytes=10)


@settings(max_examples=300, deadline=None)
@given(
    duration=st.floats(min_value=0.5, max_value=40 * 3600, allow_nan=False),
    max_bytes=st.integers(min_value=100_000, max_value=30 * 1024 * 1024),
)
def test_plan_properties(duration: float, max_bytes: int) -> None:
    plans = plan_chunks(duration, max_bytes=max_bytes)
    max_s = chunker.max_chunk_seconds(max_bytes=max_bytes)
    assert plans[0].start_s == 0
    assert plans[-1].end_s == pytest.approx(duration)
    for i, p in enumerate(plans):
        assert p.index == i
        assert 0 < p.duration_s <= max_s + 1e-6
        assert p.duration_s * BYTES_PER_S <= max_bytes  # every chunk under the byte limit
    for a, b in zip(plans, plans[1:], strict=False):
        assert b.start_s == pytest.approx(a.end_s - chunker.OVERLAP_S)  # exactly 2 s overlap
        assert b.end_s > a.end_s  # progress


# --- merging ------------------------------------------------------------------------------------------


def _words(texts_times: Sequence[tuple[str, float]]) -> list[Word]:
    return [Word(text=t, start_s=s, end_s=s + 0.3, prob=None) for t, s in texts_times]


def test_merge_offsets_and_splits_overlap_at_midpoint() -> None:
    a = ChunkPlan(0, 0.0, 100.0)
    b = ChunkPlan(1, 98.0, 150.0)
    # Overlap is 98-100 (midpoint 99). Chunk A sees words at 98.2 and 99.4; chunk B sees the same ones at 0.2 and 1.4.
    ra = ChunkResult(
        a, _words([("one", 10.0), ("two", 98.2), ("three", 99.4)]), [Segment(text="A", start_s=0, end_s=99.7)]
    )
    rb = ChunkResult(
        b, _words([("two", 0.2), ("three", 1.4), ("four", 5.0)]), [Segment(text="B", start_s=1.3, end_s=6)]
    )
    words, segments = merge_chunks([rb, ra])  # order of input does not matter
    assert [(w.text, round(w.start_s, 2)) for w in words] == [
        ("one", 10.0),
        ("two", 98.2),  # from A (before midpoint 99)
        ("three", 99.4),  # from B (after midpoint), shifted by 98
        ("four", 103.0),
    ]
    assert [(s.text, s.start_s) for s in segments] == [("A", 0.0), ("B", 99.3)]


def test_merge_drops_seam_duplicates() -> None:
    a = ChunkPlan(0, 0.0, 100.0)
    b = ChunkPlan(1, 98.0, 150.0)
    # Both chunks report "seam" straddling the midpoint with slightly different timings.
    ra = ChunkResult(a, _words([("seam", 98.95)]))
    rb = ChunkResult(b, _words([("Seam,", 1.05)]))
    words, _ = merge_chunks([ra, rb])
    assert [w.text for w in words] == ["seam"]


def test_merge_single_chunk_is_identity() -> None:
    plan = ChunkPlan(0, 0.0, 90.0)
    t = Transcript.model_validate(json.loads((FIXTURES / "transcript_talk.json").read_text()))
    words, segments = merge_chunks([ChunkResult(plan, t.words, t.segments)])
    assert words == t.words
    assert segments == t.segments


@settings(max_examples=60, deadline=None)
@given(
    duration=st.floats(min_value=30, max_value=1_500),
    max_bytes=st.integers(min_value=50_000, max_value=400_000),
    spacing=st.floats(min_value=0.5, max_value=3.0),
)
def test_merge_reconstructs_a_uniform_timeline(duration: float, max_bytes: int, spacing: float) -> None:
    """Simulate a transcriber that hears every word inside its chunk: the merge yields each word exactly once."""
    truth = [round(i * spacing, 3) for i in range(int(duration / spacing))]
    plans = plan_chunks(duration, max_bytes=max_bytes)
    results = []
    for p in plans:
        local = [(f"w{i}", t - p.start_s) for i, t in enumerate(truth) if p.start_s <= t and t + 0.3 <= p.end_s]
        results.append(ChunkResult(p, _words(local)))
    words, _ = merge_chunks(results)
    starts = [w.start_s for w in words]
    assert starts == sorted(starts)
    assert [w.text for w in words] == [f"w{i}" for i, t in enumerate(truth) if t + 0.3 <= duration]


# --- ffmpeg args and runners --------------------------------------------------------------------------


def test_extract_audio_args() -> None:
    args = audio.extract_audio_args("in.mp4", "out.mp3")
    assert args[0] == "ffmpeg"
    joined = " ".join(args)
    assert "-map 0:a:0 -vn -ac 1 -ar 16000 -b:a 32k out.mp3" in joined


def test_chunk_args() -> None:
    args = audio.chunk_args("a.mp3", ChunkPlan(2, 5600.0, 11200.5), "c.mp3")
    assert args[args.index("-ss") + 1] == "5600.000"
    assert args[args.index("-t") + 1] == "5600.500"
    assert "copy" not in args  # re-encoded: stream copy on MP3 is not frame-accurate
    assert args.index("-ss") < args.index("-i") < args.index("-t")  # accurate seek, output-side duration
    assert args[-1] == "c.mp3"


def test_cut_chunks_runner(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: Sequence[str]) -> str:
        calls.append(list(args))
        return ""

    src = tmp_path / "a.mp3"
    assert audio.cut_chunks(src, [ChunkPlan(0, 0.0, 50.0)], tmp_path / "c", runner) == [src]
    assert calls == []
    plans = plan_chunks(15_000)
    paths = audio.cut_chunks(src, plans, tmp_path / "c", runner)
    assert [p.name for p in paths] == ["chunk-000.mp3", "chunk-001.mp3", "chunk-002.mp3"]
    assert len(calls) == 3


@pytest.mark.needs_ffmpeg
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_extract_and_cut(tmp_path: Path) -> None:
    out = audio.extract_audio(FIXTURES / "synthetic_90s.mp4", tmp_path / "a.mp3")
    p = media.probe(out)
    assert p.has_audio
    assert not p.has_video
    assert p.duration_s == pytest.approx(90, abs=0.5)
    assert out.stat().st_size < 90 * BYTES_PER_S * 1.1  # ~32 kbps
    plans = plan_chunks(p.duration_s, max_bytes=200_000)  # force several chunks
    paths = audio.cut_chunks(out, plans, tmp_path / "chunks")
    for plan, path in zip(plans, paths, strict=True):
        assert media.probe(path).duration_s == pytest.approx(plan.duration_s, abs=0.2)
