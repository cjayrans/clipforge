"""D3: slot planner (pure). Invariant 11 is also covered in tests/compliance/test_inv11_slots_window.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from clipforge.compliance import ComplianceError, assert_slot_in_window
from clipforge.config import AppConfig
from clipforge.publish.slots import (
    QueuedClip,
    assign_clips,
    due_slots,
    first_slot_at,
    missed_slots,
    plan_slots,
    queue_order,
)
from clipforge.schemas import Slot

NY = ZoneInfo("America/New_York")


def _local(slot: Slot) -> time:
    return slot.at.astimezone(NY).time().replace(tzinfo=None)


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 23), [time(20, 10), time(20, 25), time(20, 40)]),  # Monday
        (date(2026, 11, 27), [time(20, 10), time(20, 25), time(20, 40)]),  # Friday
        (date(2026, 11, 28), [time(20, 30), time(20, 45)]),  # Saturday
        (date(2026, 11, 29), [time(20, 30), time(20, 45)]),  # Sunday
    ],
)
def test_plan_slots_by_day_type(app_config: AppConfig, day: date, expected: list[time]) -> None:
    slots = plan_slots(app_config, day)
    assert [_local(s) for s in slots] == expected
    assert all(s.clip_id is None for s in slots)
    assert all(s.at.tzinfo is not None and s.at.utcoffset() is not None for s in slots)


@pytest.mark.parametrize(
    ("day", "utc_offset_h"),
    [
        (date(2026, 3, 7), -5),  # EST the evening before spring-forward
        (date(2026, 3, 8), -4),  # DST starts that morning: evening is EDT
        (date(2026, 10, 31), -4),  # EDT the evening before fall-back
        (date(2026, 11, 1), -5),  # DST ended that morning: evening is EST
    ],
)
def test_plan_slots_across_dst(app_config: AppConfig, day: date, utc_offset_h: int) -> None:
    for slot in plan_slots(app_config, day):
        assert slot.at.utcoffset() == timedelta(hours=utc_offset_h)
        window = app_config.window_for(day)
        assert_slot_in_window(slot.at, window.start, window.end, NY)


def test_plan_slots_utc_instant_is_correct(app_config: AppConfig) -> None:
    # 20:10 EST on 2026-11-23 is 01:10 UTC on the 24th.
    assert plan_slots(app_config, date(2026, 11, 23))[0].at == datetime(2026, 11, 24, 1, 10, tzinfo=UTC)


@given(st.dates(min_value=date(2026, 1, 1), max_value=date(2030, 12, 31)))
def test_every_planned_slot_is_inside_its_window(day: date) -> None:
    from clipforge.config import load_app_config
    from tests.conftest import EXAMPLE_CONFIG

    cfg = load_app_config(EXAMPLE_CONFIG)
    window = cfg.window_for(day)
    slots = plan_slots(cfg, day)
    assert slots == sorted(slots, key=lambda s: s.at)
    for slot in slots:
        assert_slot_in_window(slot.at, window.start, window.end, cfg.tz)


def test_plan_slots_refuses_a_slot_outside_the_window(app_config: AppConfig) -> None:
    # AppConfig is frozen and validated, so simulate a bypass: the planner re-checks invariant 11 itself.
    bad = app_config.model_copy(update={"slots": app_config.slots.model_copy(update={"weekdays": [time(22, 0)]})})
    with pytest.raises(ComplianceError, match="#11"):
        plan_slots(bad, date(2026, 11, 23))


def _q(clip_id: int, priority: int, minute: int) -> QueuedClip:
    return QueuedClip(clip_id, priority, datetime(2026, 11, 23, 20, minute, tzinfo=NY))


def test_queue_order_priority_then_fifo() -> None:
    queue = [_q(1, 0, 5), _q(2, 5, 9), _q(3, 0, 1), _q(4, 5, 2)]
    assert [q.clip_id for q in queue_order(queue)] == [4, 2, 3, 1]


def test_assign_clips_one_per_slot_in_order(app_config: AppConfig) -> None:
    slots = plan_slots(app_config, date(2026, 11, 23))
    out = assign_clips(slots, [_q(1, 0, 5), _q(2, 5, 9), _q(3, 0, 1), _q(4, 5, 2)])
    assert [s.clip_id for s in out] == [4, 2, 3]  # clip 1 has no slot left
    assert [s.at for s in out] == [s.at for s in slots]
    assert all(s.clip_id is None for s in slots)  # inputs untouched


def test_assign_clips_keeps_existing_and_never_duplicates(app_config: AppConfig) -> None:
    slots = plan_slots(app_config, date(2026, 11, 23))
    first = assign_clips(slots, [_q(1, 0, 1)])
    again = assign_clips(first, [_q(1, 0, 1), _q(2, 0, 2)])
    assert [s.clip_id for s in again] == [1, 2, None]
    assert assign_clips(again, [_q(1, 0, 1), _q(2, 0, 2)]) == again


def test_assign_clips_empty_queue(app_config: AppConfig) -> None:
    slots = plan_slots(app_config, date(2026, 11, 23))
    assert assign_clips(slots, []) == slots


def test_due_slots_window(app_config: AppConfig) -> None:
    slots = assign_clips(plan_slots(app_config, date(2026, 11, 23)), [_q(1, 0, 1), _q(2, 0, 2)])
    first = slots[0].at
    assert due_slots(slots, first - timedelta(seconds=61)) == []
    assert [s.clip_id for s in due_slots(slots, first - timedelta(seconds=60))] == [1]
    assert [s.clip_id for s in due_slots(slots, first + timedelta(minutes=5))] == [1]
    assert due_slots(slots, first + timedelta(minutes=5, seconds=1)) == []
    # slot 3 is empty, so it is never due
    assert due_slots(slots, slots[2].at) == []


def test_missed_and_first_slot(app_config: AppConfig) -> None:
    slots = plan_slots(app_config, date(2026, 11, 23))
    assert first_slot_at(slots) == slots[0].at
    assert first_slot_at([]) is None
    assert missed_slots(slots, slots[0].at + timedelta(minutes=5)) == []
    assert missed_slots(slots, slots[0].at + timedelta(minutes=6)) == [slots[0]]
