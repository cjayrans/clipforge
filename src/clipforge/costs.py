"""Cost ledger and monthly spend cap (PRD F10, COMPLIANCE #12).

Every Anthropic / Groq call records its usage with `record()`. Before each paid stage the pipeline calls `guard()`,
which raises `SpendCapReached` when the pipeline is paused or month-to-date spend has reached
`monthly_api_cap_usd`. The pipeline catches it and calls `pause()`; `cf resume` calls `resume()`.

`resume()` only clears the pause flag. If month-to-date spend is still at or over the cap, the next `guard()` raises
again — to keep going in the same month, raise `monthly_api_cap_usd` in config first.

Months are calendar months in the operator's timezone (`AppConfig.timezone`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from clipforge.config import AppConfig, Price
from clipforge.db.models import CostRow, FlagRow, utcnow
from clipforge.db.transitions import Actor, record_event

PAUSE_FLAG = "pipeline_paused"

# unit_kind -> (Price field, units per priced quantity)
UNIT_PRICING: dict[str, tuple[str, float]] = {
    "input_tokens": ("input_per_mtok", 1_000_000),
    "output_tokens": ("output_per_mtok", 1_000_000),
    "audio_seconds": ("per_audio_hour", 3600),
}


class SpendCapReached(RuntimeError):
    def __init__(self, month_to_date_usd: float, cap_usd: float, *, paused_reason: str | None = None) -> None:
        self.month_to_date_usd = month_to_date_usd
        self.cap_usd = cap_usd
        self.paused_reason = paused_reason
        if paused_reason is not None:
            msg = f"pipeline is paused ({paused_reason}); run `cf resume`"
        else:
            msg = f"monthly API spend ${month_to_date_usd:.2f} reached the cap ${cap_usd:.2f}"
        super().__init__(msg)


class UnknownPrice(KeyError):
    pass


def price_usd(app: AppConfig, model: str, units: float, unit_kind: str) -> float:
    """USD for `units` of `unit_kind` on `model`, from `config.prices`."""
    if units < 0:
        raise ValueError("units must be >= 0")
    price: Price | None = app.prices.get(model)
    if price is None:
        raise UnknownPrice(f"no price configured for model {model!r}")
    try:
        field, per = UNIT_PRICING[unit_kind]
    except KeyError as exc:
        raise UnknownPrice(f"unknown unit_kind {unit_kind!r}; expected one of {sorted(UNIT_PRICING)}") from exc
    rate = getattr(price, field)
    if rate is None:
        raise UnknownPrice(f"model {model!r} has no {field} price")
    return float(rate) * units / per


def record(
    session: Session,
    app: AppConfig,
    service: str,
    model: str,
    units: float,
    unit_kind: str,
    *,
    at: datetime | None = None,
) -> CostRow:
    """Append a cost row priced from config. Flushes; the caller commits."""
    row = CostRow(
        at=at or utcnow(),
        service=service,
        model=model,
        units=units,
        unit_kind=unit_kind,
        usd=price_usd(app, model, units, unit_kind),
    )
    session.add(row)
    session.flush()
    return row


def month_start_utc(now: datetime, tz: ZoneInfo) -> datetime:
    local = now.astimezone(tz)
    start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC)


def month_to_date(session: Session, app: AppConfig, *, now: datetime | None = None) -> float:
    now = now or utcnow()
    start = month_start_utc(now, app.tz)
    total = session.scalar(
        select(func.coalesce(func.sum(CostRow.usd), 0.0)).where(CostRow.at >= start, CostRow.at <= now)
    )
    return float(total or 0.0)


def is_paused(session: Session) -> str | None:
    """The pause reason, or None when not paused."""
    flag = session.get(FlagRow, PAUSE_FLAG)
    return flag.value if flag is not None else None


def guard(session: Session, app: AppConfig, *, now: datetime | None = None) -> float:
    """Raise SpendCapReached if paused or at/over the monthly cap; otherwise return month-to-date USD."""
    mtd = month_to_date(session, app, now=now)
    reason = is_paused(session)
    if reason is not None:
        raise SpendCapReached(mtd, app.monthly_api_cap_usd, paused_reason=reason)
    if mtd >= app.monthly_api_cap_usd:
        raise SpendCapReached(mtd, app.monthly_api_cap_usd)
    return mtd


def pause(session: Session, reason: str, *, actor: Actor = "system") -> None:
    """Set the pause flag and write an events row. Idempotent."""
    flag = session.get(FlagRow, PAUSE_FLAG)
    if flag is None:
        session.add(FlagRow(key=PAUSE_FLAG, value=reason))
    else:
        flag.value = reason
    record_event(session, "pipeline", "nightly", "running", "paused", actor=actor, detail=reason)
    session.flush()


def resume(session: Session, *, actor: Actor = "operator") -> bool:
    """Clear the pause flag. Returns False if it was not paused."""
    flag = session.get(FlagRow, PAUSE_FLAG)
    if flag is None:
        return False
    session.delete(flag)
    record_event(session, "pipeline", "nightly", "paused", "running", actor=actor, detail="cf resume")
    session.flush()
    return True
