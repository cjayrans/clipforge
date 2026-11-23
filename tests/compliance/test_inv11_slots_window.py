"""Invariant 11: publish slots fall inside the operator's configured window."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest
import yaml

from clipforge.compliance import ComplianceError, assert_slot_in_window
from clipforge.config import AppConfig
from tests.conftest import EXAMPLE_CONFIG

NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("day", "slots"),
    [("weekdays", ["20:10", "21:05"]), ("saturday", ["19:59"]), ("sunday", ["21:00"])],
)
def test_slots_inside_operator_window(day: str, slots: list[str]) -> None:
    data = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    data["slots"][day] = slots
    with pytest.raises(ValueError, match="COMPLIANCE #11"):
        AppConfig.model_validate(data)


def test_assert_slot_in_window_uses_local_time() -> None:
    start, end = time(20, 0), time(21, 0)
    # 20:10 New York in winter (EST) and summer (EDT).
    assert_slot_in_window(datetime(2026, 11, 24, 1, 10, tzinfo=UTC), start, end, NY)
    assert_slot_in_window(datetime(2026, 7, 1, 0, 10, tzinfo=UTC), start, end, NY)
    with pytest.raises(ComplianceError):
        assert_slot_in_window(datetime(2026, 11, 24, 2, 0, tzinfo=UTC), start, end, NY)  # 21:00, end exclusive
    with pytest.raises(ComplianceError):
        assert_slot_in_window(datetime(2026, 11, 24, 0, 59, tzinfo=UTC), start, end, NY)  # 19:59
    with pytest.raises(ComplianceError, match="timezone-aware"):
        assert_slot_in_window(datetime(2026, 11, 23, 20, 10), start, end, NY)


def test_planned_slots_inside_window(app_config: AppConfig) -> None:
    from clipforge.publish.slots import plan_slots  # type: ignore[attr-defined]

    for d in (date(2026, 11, 23), date(2026, 11, 28), date(2026, 11, 29)):
        window = app_config.window_for(d)
        for slot in plan_slots(app_config, d):
            assert_slot_in_window(slot.at, window.start, window.end, app_config.tz)
