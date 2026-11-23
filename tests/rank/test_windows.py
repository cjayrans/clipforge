"""B1: window builder (pure). Property tests on boundaries and lengths, plus the fixture transcripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clipforge.rank.windows import (
    WINDOW_MAX_S,
    WINDOW_MIN_S,
    build_windows,
    prescore,
    sentence_units,
)
from clipforge.schemas import Segment, Transcript, Word

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EPS = 1e-6


def load(name: str) -> Transcript:
    return Transcript.model_validate(json.loads((FIXTURES / name).read_text()))


def make_transcript(words: list[Word], segments: list[Segment] | None = None, source_id: int = 7) -> Transcript:
    end = max((w.end_s for w in words), default=0.0)
    return Transcript(
        source_id=source_id,
        provider="groq",
        model="whisper-large-v3-turbo",
        words=words,
        segments=segments or [],
        duration_s=end + 1.0,
    )


# word = (gap before, duration, text, punctuation)
word_specs = st.lists(
    st.tuples(
        st.floats(min_value=0.0, max_value=4.0, allow_nan=False),
        st.floats(min_value=0.05, max_value=1.5, allow_nan=False),
        st.sampled_from(["go", "wait", "chat", "why", "car", "okay"]),
        st.sampled_from(["", "", "", ".", "?", "!", ","]),
    ),
    min_size=0,
    max_size=250,
)


def build_words(specs: list[tuple[float, float, str, str]]) -> list[Word]:
    t = 0.0
    words = []
    for gap, dur, text, punct in specs:
        t += gap
        words.append(Word(text=text + punct, start_s=round(t, 3), end_s=round(t + dur, 3)))
        t += dur
    return words


@settings(max_examples=150, deadline=None)
@given(
    specs=word_specs,
    min_s=st.sampled_from([20.0, 25.0]),
    max_s=st.sampled_from([45.0, 60.0, 75.0]),
    stride=st.sampled_from([5.0, 10.0, 15.0]),
    keep=st.integers(min_value=1, max_value=80),
)
def test_window_properties(specs, min_s, max_s, stride, keep):
    tr = make_transcript(build_words(specs))
    units = sentence_units(tr, max_unit_s=max_s)
    unit_starts = {u.start_s for u in units}
    unit_ends = {u.end_s for u in units}
    last_end = max((w.end_s for w in tr.words), default=0.0)

    windows = build_windows(tr, min_s=min_s, max_s=max_s, stride_s=stride, keep=keep)

    assert len(windows) <= keep
    assert len({w.window_id for w in windows}) == len(windows)
    for w in windows:
        assert min_s - EPS <= w.end_s - w.start_s <= max_s + EPS
        assert WINDOW_MIN_S <= w.end_s - w.start_s <= WINDOW_MAX_S
        assert w.start_s in unit_starts
        assert w.end_s in unit_ends
        assert 0.0 <= w.start_s < w.end_s <= last_end + EPS
        assert 0.0 <= w.prescore <= 1.0
        assert w.source_id == tr.source_id
        assert w.text.strip()
    scores = [w.prescore for w in windows]
    assert scores == sorted(scores, reverse=True)
    starts = sorted({w.start_s for w in windows})
    assert all(b - a >= stride - EPS for a, b in zip(starts, starts[1:], strict=False))
    # Deterministic.
    assert build_windows(tr, min_s=min_s, max_s=max_s, stride_s=stride, keep=keep) == windows


@settings(max_examples=100, deadline=None)
@given(specs=word_specs, max_unit=st.sampled_from([20.0, 45.0, 75.0]))
def test_units_partition_words_in_order(specs, max_unit):
    words = build_words(specs)
    units = sentence_units(make_transcript(words), max_unit_s=max_unit)
    for u in units:
        assert u.end_s - u.start_s <= max_unit + EPS
        assert u.n_words >= 1
    assert [u.start_s for u in units] == sorted(u.start_s for u in units)
    assert sum(u.n_words for u in units) <= len(words)


def test_short_transcript_has_no_windows():
    words = build_words([(0.1, 0.4, "hello", "."), (0.2, 0.4, "bye", ".")] * 10)
    assert max(w.end_s for w in words) < 20
    assert build_windows(make_transcript(words)) == []


def test_empty_transcript_has_no_windows():
    assert build_windows(make_transcript([])) == []


def test_talk_fixture_windows():
    tr = load("transcript_talk.json")
    windows = build_windows(tr, keep=60)
    assert windows, "dense commentary must yield windows"
    for w in windows:
        assert 20.0 <= w.end_s - w.start_s <= 75.0
        assert w.window_id == f"1:{round(w.start_s * 1000)}-{round(w.end_s * 1000)}"
    # Windows start on sentence starts and end on sentence ends.
    assert all(w.text[0].isupper() and w.text[-1] in ".?!" for w in windows)


def test_sparse_fixture_windows_have_low_density():
    sparse = build_windows(load("transcript_sparse.json"))
    talk = build_windows(load("transcript_talk.json"))
    assert sparse
    assert max(w.prescore for w in sparse) < max(w.prescore for w in talk)


def test_keep_limits_output():
    assert len(build_windows(load("transcript_talk.json"), keep=3)) == 3


def test_no_punctuation_falls_back_to_segments():
    words = build_words([(0.05, 0.3, "go", "")] * 200)
    segs = [Segment(text="x", start_s=words[k].start_s, end_s=words[k + 9].end_s) for k in range(0, 200, 10)]
    units = sentence_units(make_transcript(words, segs))
    assert len(units) == 20
    assert all(u.n_words == 10 for u in units)


def test_no_word_timestamps_uses_segments():
    segs = [Segment(text=f"Sentence {k}.", start_s=k * 6.0, end_s=k * 6.0 + 5.0) for k in range(15)]
    windows = build_windows(make_transcript([], segs))
    assert windows
    assert all(w.start_s % 6.0 == 0 for w in windows)


def test_long_sentence_is_split():
    words = build_words([(0.0, 0.5, "go", "")] * 300)  # 150 s, no punctuation, no segments
    units = sentence_units(make_transcript(words), max_unit_s=75.0)
    assert len(units) >= 2
    assert all(u.end_s - u.start_s <= 75.0 for u in units)


def test_question_answer_scores_higher():
    def units_of(texts):
        return sentence_units(make_transcript(build_words([(0.1, 0.5, t, p) for t, p in texts])))

    def score(units):
        return prescore(units, units[0].start_s, units[-1].end_s, None)

    assert score(units_of([("why", "?"), ("car", ".")] * 20)) > score(units_of([("why", "."), ("car", ".")] * 20))


def test_energy_peaks_raise_prescore():
    tr = load("transcript_talk.json")
    flat = {w.window_id: w.prescore for w in build_windows(tr, energy_per_s=[-30.0] * 90)}
    loud_end = build_windows(tr, energy_per_s=[-30.0] * 60 + [-5.0] * 30)
    late = [w for w in loud_end if w.end_s > 65 and w.window_id in flat]
    assert late
    assert all(w.prescore > flat[w.window_id] for w in late)


def test_energy_constant_or_empty_is_neutral():
    tr = load("transcript_talk.json")
    assert build_windows(tr, energy_per_s=[]) == build_windows(tr, energy_per_s=[-20.0] * 90)


@pytest.mark.parametrize(("min_s", "max_s"), [(10.0, 60.0), (30.0, 30.0), (20.0, 90.0)])
def test_invalid_bounds_raise(min_s, max_s):
    with pytest.raises(ValueError, match="min_s"):
        build_windows(load("transcript_talk.json"), min_s=min_s, max_s=max_s)
