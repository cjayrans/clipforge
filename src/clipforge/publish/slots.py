"""Slot planning (pure, no I/O): when tonight's slots are and which approved clip goes in which slot.

Docs: docs/ARCHITECTURE.md §4.7, PRD §4.3. Invariant 11 (COMPLIANCE.md): every slot falls inside the operator's
window; `plan_slots` re-checks each slot with `compliance.assert_slot_in_window` even though `AppConfig` already
validated the times, so a slot outside the window can never leave this module.

All datetimes are timezone-aware in `AppConfig.tz` (America/New_York). `datetime.combine(day, t, tzinfo=tz)` with a
`ZoneInfo` resolves DST correctly; slot times (evening) never fall in a DST gap.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from clipforge.compliance import assert_slot_in_window
from clipforge.config import AppConfig
from clipforge.schemas import Slot

# A slot is due this long before its time, so the 30 s dispatcher tick never misses it.
DUE_LEAD = timedelta(seconds=60)
# A slot whose time passed longer ago than this (e.g. the worker was down) is not published late.
LATE_GRACE = timedelta(minutes=5)


@dataclass(frozen=True)
class QueuedClip:
    """An approved clip waiting for a slot. Ordered by campaign priority (high first), then approval time (FIFO)."""

    clip_id: int
    campaign_priority: int
    approved_at: datetime


def plan_slots(app_config: AppConfig, day: date) -> list[Slot]:
    """Tonight's slots for `day` (an America/New_York calendar date), earliest first, with no clip assigned."""
    window = app_config.window_for(day)
    slots: list[Slot] = []
    for t in app_config.slots_for(day):
        at = datetime.combine(day, t, tzinfo=app_config.tz)
        assert_slot_in_window(at, window.start, window.end, app_config.tz)
        slots.append(Slot(at=at))
    return slots


def queue_order(queue: Iterable[QueuedClip]) -> list[QueuedClip]:
    """FIFO by campaign priority: higher priority first, then earlier approval, then lower clip id (stable)."""
    return sorted(queue, key=lambda q: (-q.campaign_priority, q.approved_at, q.clip_id))


def assign_clips(slots: Sequence[Slot], queue: Iterable[QueuedClip]) -> list[Slot]:
    """Fill empty slots (earliest first) from `queue`; one clip per slot, a clip never twice.

    Slots that already hold a clip are kept as they are, and clips already in a slot are skipped in the queue, so this
    can run on every dispatcher tick as new approvals arrive. Returns new `Slot` objects; the inputs are unchanged.
    """
    taken = {s.clip_id for s in slots if s.clip_id is not None}
    waiting = [q for q in queue_order(queue) if q.clip_id not in taken]
    out: list[Slot] = []
    for slot in sorted(slots, key=lambda s: s.at):
        if slot.clip_id is None and waiting:
            out.append(Slot(at=slot.at, clip_id=waiting.pop(0).clip_id))
        else:
            out.append(Slot(at=slot.at, clip_id=slot.clip_id))
    return out


def due_slots(
    slots: Iterable[Slot], now: datetime, *, lead: timedelta = DUE_LEAD, late_grace: timedelta = LATE_GRACE
) -> list[Slot]:
    """Slots with a clip whose time is within `lead` ahead of `now` or at most `late_grace` behind it."""
    return [
        s
        for s in sorted(slots, key=lambda s: s.at)
        if s.clip_id is not None and s.at - lead <= now <= s.at + late_grace
    ]


def missed_slots(slots: Iterable[Slot], now: datetime, *, late_grace: timedelta = LATE_GRACE) -> list[Slot]:
    """Slots whose time is further in the past than `late_grace` (too late to publish)."""
    return [s for s in sorted(slots, key=lambda s: s.at) if now > s.at + late_grace]


def first_slot_at(slots: Sequence[Slot]) -> datetime | None:
    return min((s.at for s in slots), default=None)
