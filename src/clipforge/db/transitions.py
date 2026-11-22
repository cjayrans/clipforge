"""The only way to change a row's `state`: checks the schemas transition tables and writes an `events` row."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from sqlalchemy.orm import Session

from clipforge.db.models import (
    IN_TRANSITION,
    CandidateRow,
    ClipRow,
    EventRow,
    IllegalTransition,
    PostRow,
    SubmissionRow,
)

Actor = Literal["system", "operator"]
Stateful = CandidateRow | ClipRow | PostRow | SubmissionRow

_ENTITY: dict[type, str] = {CandidateRow: "candidate", ClipRow: "clip", PostRow: "post", SubmissionRow: "submission"}


def record_event(
    session: Session,
    entity: str,
    entity_id: int | str,
    from_state: str | None,
    to_state: str | None,
    *,
    actor: Actor = "system",
    detail: str = "",
) -> EventRow:
    """Append an audit row. Use directly for non-state events (e.g. pause, arm, approval)."""
    ev = EventRow(
        entity=entity,
        entity_id=str(entity_id),
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        detail=detail,
    )
    session.add(ev)
    return ev


def transition(
    session: Session, row: Stateful, to_state: StrEnum, *, actor: Actor = "system", detail: str = ""
) -> None:
    """Move `row` to `to_state` if the transition table allows it; write an `events` row. Flushes the session.

    Raises `IllegalTransition` otherwise. The row must already be persisted (have an id).
    """
    if row.id is None:
        session.flush()
    old = row.state
    if old == to_state:
        raise IllegalTransition(f"{type(row).__name__} {row.id} is already {to_state}")
    token = IN_TRANSITION.set(True)
    try:
        row.state = to_state  # type: ignore[assignment]  # validated against the right table by the model
    finally:
        IN_TRANSITION.reset(token)
    record_event(session, _ENTITY[type(row)], row.id, str(old), str(to_state), actor=actor, detail=detail)
    session.flush()
