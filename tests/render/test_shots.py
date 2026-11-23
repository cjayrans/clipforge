from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.render.shots import Shot, detect_shots, normalize_shots
from tests.conftest import FIXTURES


def test_no_cuts_is_one_shot() -> None:
    assert normalize_shots([], 10.0, 40.0) == [Shot(10.0, 40.0)]


def test_cuts_clipped_to_range() -> None:
    shots = normalize_shots([(0.0, 30.0), (30.0, 60.0), (60.0, 90.0)], 20.0, 70.0)
    assert shots == [Shot(20.0, 30.0), Shot(30.0, 60.0), Shot(60.0, 70.0)]


def test_short_shot_merged_into_previous() -> None:
    shots = normalize_shots([(0.0, 30.0), (30.0, 30.4), (30.4, 90.0)], 20.0, 50.0)
    assert shots == [Shot(20.0, 30.4), Shot(30.4, 50.0)]


def test_short_first_shot_merged_into_next() -> None:
    assert normalize_shots([(0.0, 20.3)], 20.0, 50.0) == [Shot(20.0, 50.0)]


def test_empty_range_rejected() -> None:
    with pytest.raises(ValueError, match="empty range"):
        normalize_shots([], 5.0, 5.0)


@given(
    start=st.floats(0, 100),
    length=st.floats(1.5, 90),
    cuts=st.lists(st.floats(0, 200), max_size=20),
    min_shot=st.floats(0.1, 1.0),
)
def test_shots_contiguous_and_cover_range(start: float, length: float, cuts: list[float], min_shot: float) -> None:
    end = start + length
    shots = normalize_shots([(c, c) for c in cuts], start, end, min_shot)
    assert shots[0].start_s == start
    assert shots[-1].end_s == end
    for a, b in zip(shots, shots[1:], strict=False):
        assert a.end_s == b.start_s
    assert all(s.duration_s > 0 for s in shots)
    if len(shots) > 1:
        assert all(s.duration_s >= min_shot for s in shots[1:-1])


def test_detect_shots_on_fixture() -> None:
    pytest.importorskip("cv2")
    shots = detect_shots(FIXTURES / "synthetic_90s.mp4", 20.0, 70.0)
    assert len(shots) == 3
    assert shots[0].start_s == 20.0
    assert shots[-1].end_s == 70.0
    assert shots[1].start_s == pytest.approx(30.0, abs=0.1)
    assert shots[2].start_s == pytest.approx(60.0, abs=0.1)
