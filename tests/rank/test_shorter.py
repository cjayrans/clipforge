"""Phase 2: `rank.select.shorter_range` (the bot's Shorter button), pure."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.rank.select import shorter_range
from clipforge.schemas import Word


def words_every(start: float, end: float, step: float = 0.5, text: str = "word") -> list[Word]:
    out: list[Word] = []
    t = start
    while t + 0.3 <= end:
        out.append(Word(text=text, start_s=t, end_s=t + 0.3))
        t += step
    return out


def test_dense_tail_quiet_head_cuts_the_head() -> None:
    # 0-40 s: the first 10 s are nearly silent, the rest is dense speech.
    words = [Word(text="um.", start_s=1.0, end_s=1.3), *words_every(10.0, 40.0)]
    start, end = shorter_range(words, 0.0, 40.0, min_clip_s=10)
    assert end == 40.0
    assert start >= 8.0  # at least 20% (8 s) gone from the head
    assert end - start <= 32.0 + 1e-6


def test_quiet_tail_cuts_the_tail() -> None:
    words = [*words_every(0.0, 30.0), Word(text="bye.", start_s=38.0, end_s=38.3)]
    start, end = shorter_range(words, 0.0, 40.0, min_clip_s=10)
    assert start == 0.0
    assert end <= 32.0


def test_tail_cut_snaps_to_a_sentence_end() -> None:
    # A dense head makes the tail the weaker end; a sentence ends at 29.8 s, 2.2 s before the plain 20% cut.
    words = [*words_every(0.0, 10.0, step=0.25), *words_every(10.0, 40.0)]
    i = next(i for i, w in enumerate(words) if w.start_s == 29.5)
    words[i] = Word(text="done.", start_s=29.5, end_s=29.8)
    start, end = shorter_range(words, 0.0, 40.0, min_clip_s=10)
    assert start == 0.0
    assert end == pytest.approx(29.8)


def test_too_short_to_trim_refused() -> None:
    with pytest.raises(ValueError, match="minimum"):
        shorter_range(words_every(0.0, 12.0), 0.0, 12.0, min_clip_s=10)


def test_no_words_cuts_the_tail_by_exactly_20_percent() -> None:
    assert shorter_range([], 5.0, 45.0, min_clip_s=10) == (5.0, 37.0)


@given(
    start=st.floats(0, 100),
    length=st.floats(13, 90),
    step=st.floats(0.2, 3.0),
)
def test_result_is_shorter_inside_and_long_enough(start: float, length: float, step: float) -> None:
    end = start + length
    words = words_every(start, end, step)
    new_start, new_end = shorter_range(words, start, end, min_clip_s=10)
    assert start - 1e-6 <= new_start < new_end <= end + 1e-6
    assert new_end - new_start <= length * 0.8 + 1e-3
    assert new_end - new_start >= 10 - 1e-3
    # One end is untouched.
    assert new_start == start or new_end == end
