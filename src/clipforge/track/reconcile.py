"""Reconcile submissions with what Content Rewards shows (PRD §4.4): CSV round trip and the bot's /reconcile walk.

Content Rewards has no API and its terms don't allow scraping, so the operator reads the dashboard and tells us:
either with `/reconcile` in the bot, or by `cf export-reconcile` -> edit the CSV -> `cf import-reconcile`.

CSV columns are `schemas.ReconcileRow`. Rules:
- Export lists open submissions (awaiting or submitted, brand decision not yet rejected/paid), oldest first, with the
  latest view count. dry_run posts (fake `.invalid` URLs) are never exported.
- Import validates every row first and applies nothing if any row is bad (the error lists line numbers). The campaign
  name, platform and post URL in each row must match the database, so an edited or stale file cannot hit the wrong row.
- `paid_usd` in the CSV is the TOTAL paid for that submission (it sets, it does not add), so re-importing the same file
  changes nothing. `views` adds a manual view snapshot only when it differs from the latest one.
- A row with `submitted_at` set moves an AWAITING submission to SUBMITTED. A MISSED submission cannot be edited.
"""

from __future__ import annotations

import csv
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.db import CampaignRow, FlagRow, PostRow, SubmissionRow, ViewSnapshotRow, record_event, transition
from clipforge.schemas import BrandDecision, ReconcileRow, SubmissionState
from clipforge.track.metrics import is_fake_url, latest_views

log = logging.getLogger(__name__)

CSV_FIELDS = [
    "submission_id",
    "campaign_name",
    "platform",
    "post_url",
    "submitted_at",
    "brand_decision",
    "views",
    "paid_usd",
    "note",
]
PENDING_AMOUNT_FLAG = "reconcile_pending_amount"
_AMOUNT_RE = re.compile(r"^\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?|\.[0-9]+)\s*$")


class ReconcileError(Exception):
    """Bad CSV or an impossible update. `import_csv` (the CLI target) prints it as `Error: ...` and exits 1."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def parse_amount(text: str) -> float:
    """'$12.50', '1,234', '0' -> dollars. Raises ValueError for anything else (including negatives)."""
    match = _AMOUNT_RE.match(text)
    if match is None:
        raise ValueError("amount must be a number like 12.50 or 0")
    value = float(match.group(1).replace(",", ""))
    if math.isnan(value) or math.isinf(value):
        raise ValueError("amount must be a finite number")
    return value


# --------------------------------------------------------------------------------------------------
# Which submissions are open
# --------------------------------------------------------------------------------------------------


def _all_open(session: Session) -> list[SubmissionRow]:
    rows = session.scalars(
        select(SubmissionRow)
        .where(
            SubmissionRow.state.in_([SubmissionState.AWAITING, SubmissionState.SUBMITTED]),
            SubmissionRow.brand_decision.in_([BrandDecision.PENDING, BrandDecision.APPROVED]),
        )
        .order_by(SubmissionRow.deadline_at, SubmissionRow.id)
    ).all()
    out: list[SubmissionRow] = []
    for sub in rows:
        post = session.get(PostRow, sub.post_id)
        if post is not None and not is_fake_url(post.post_url):
            out.append(sub)
    return out


def open_submissions(session: Session) -> list[SubmissionRow]:
    """Submitted, not yet decided or paid: what `/reconcile` walks through, oldest first."""
    return [s for s in _all_open(session) if s.state is SubmissionState.SUBMITTED]


# --------------------------------------------------------------------------------------------------
# One update (shared by the CSV import and the bot)
# --------------------------------------------------------------------------------------------------


def apply_brand_update(
    session: Session, sub: SubmissionRow, decision: BrandDecision, paid_usd: float | None, now: datetime
) -> bool:
    """Set the brand decision and (optionally) the total paid. Returns True when something changed."""
    if sub.state is SubmissionState.MISSED:
        raise ReconcileError(f"submission {sub.id} was marked missed and cannot be reconciled")
    if sub.state is SubmissionState.AWAITING and decision is not BrandDecision.PENDING:
        raise ReconcileError(f"submission {sub.id} was never marked submitted; mark it submitted first")
    if paid_usd is not None and paid_usd < 0:
        raise ReconcileError(f"submission {sub.id}: paid_usd cannot be negative")
    if decision is BrandDecision.REJECTED and (paid_usd or 0) > 0:
        raise ReconcileError(f"submission {sub.id}: a rejected submission cannot have a payout")
    changed = False
    if decision is not sub.brand_decision:
        record_event(
            session,
            "submission",
            sub.id,
            sub.brand_decision.value,
            decision.value,
            actor="operator",
            detail="brand decision",
        )
        if sub.brand_decision is BrandDecision.PENDING and sub.decided_at is None:
            sub.decided_at = now
        sub.brand_decision = decision
        changed = True
    if paid_usd is not None and abs(paid_usd - sub.paid_usd) > 1e-9:
        record_event(
            session,
            "submission",
            sub.id,
            f"paid={sub.paid_usd:.2f}",
            f"paid={paid_usd:.2f}",
            actor="operator",
            detail="paid total",
        )
        sub.paid_usd = paid_usd
        changed = True
    session.flush()
    return changed


# --------------------------------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------------------------------


def export_csv(session: Session, settings: Settings, path: Path) -> int:
    """Write open submissions to `path`. Returns the number of rows."""
    rows = _all_open(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for sub in rows:
            post = session.get(PostRow, sub.post_id)
            campaign = session.get(CampaignRow, sub.campaign_id)
            assert post is not None
            assert campaign is not None
            views = latest_views(session, post.id)
            writer.writerow(
                {
                    "submission_id": sub.id,
                    "campaign_name": campaign.name,
                    "platform": post.platform.value,
                    "post_url": post.post_url or "",
                    "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else "",
                    "brand_decision": sub.brand_decision.value,
                    "views": "" if views is None else views,
                    "paid_usd": f"{sub.paid_usd:.2f}",
                    "note": sub.note,
                }
            )
    return len(rows)


@dataclass
class _Parsed:
    line: int
    row: ReconcileRow


def _clean(raw: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key in CSV_FIELDS:
        value = raw.get(key)
        text = "" if value is None else str(value).strip()
        data[key] = text if text != "" else None
    data["note"] = (raw.get("note") or "").strip()
    if data["paid_usd"] is not None:
        data["paid_usd"] = parse_amount(str(data["paid_usd"]))
    return data


def _reason(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return str(exc)


def _read(path: Path) -> list[_Parsed]:
    errors: list[str] = []
    parsed: list[_Parsed] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        missing = [f for f in CSV_FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            raise ReconcileError(f"{path}: missing column(s): {', '.join(missing)}")
        for line, raw in enumerate(reader, start=2):
            try:
                row = ReconcileRow.model_validate(_clean(raw))
                if row.submitted_at is not None and row.submitted_at.tzinfo is None:
                    raise ValueError("submitted_at needs a timezone offset, e.g. 2026-11-24T01:12:00+00:00")
                parsed.append(_Parsed(line, row))
            except (ValidationError, ValueError) as exc:
                errors.append(f"line {line}: {_reason(exc)}")
    if errors:
        raise ReconcileError("nothing imported:\n  " + "\n  ".join(errors))
    return parsed


def import_csv(session: Session, settings: Settings, path: Path) -> int:
    """`cf import-reconcile`: `import_rows`, with a bad file reported as `Error: ...` (exit 1), not a traceback.

    (Typer 0.29 bundles its own Click, so it does not recognise `click.ClickException`; it is handled here.)
    """
    try:
        return import_rows(session, settings, path)
    except ReconcileError as exc:
        typer.echo(f"Error: {exc.message}", err=True)
        raise typer.Exit(1) from exc


def import_rows(session: Session, settings: Settings, path: Path) -> int:
    """Apply an edited reconcile CSV. All rows are validated first; none is applied if any is bad.

    Returns the number of submissions that changed (0 when the file matches the database).
    Raises `ReconcileError` for a bad file.
    """
    parsed = _read(path)
    now = datetime.now(UTC)
    errors: list[str] = []
    seen: set[int] = set()
    targets: list[tuple[SubmissionRow, PostRow, ReconcileRow, int]] = []
    for item in parsed:
        row = item.row
        sub = session.get(SubmissionRow, row.submission_id)
        if sub is None:
            errors.append(f"line {item.line}: no submission {row.submission_id}")
            continue
        if row.submission_id in seen:
            errors.append(f"line {item.line}: submission {row.submission_id} appears twice")
            continue
        seen.add(row.submission_id)
        post = session.get(PostRow, sub.post_id)
        campaign = session.get(CampaignRow, sub.campaign_id)
        assert post is not None
        assert campaign is not None
        if (campaign.name, post.platform, post.post_url) != (row.campaign_name, row.platform, row.post_url):
            errors.append(f"line {item.line}: campaign/platform/post_url do not match submission {sub.id}")
            continue
        if sub.state is SubmissionState.MISSED:
            errors.append(f"line {item.line}: submission {sub.id} was marked missed and cannot be reconciled")
            continue
        if row.brand_decision is BrandDecision.REJECTED and (row.paid_usd or 0) > 0:
            errors.append(f"line {item.line}: a rejected submission cannot have a payout")
            continue
        will_be_submitted = sub.state is SubmissionState.SUBMITTED or row.submitted_at is not None
        if not will_be_submitted and row.brand_decision is not BrandDecision.PENDING:
            errors.append(f"line {item.line}: submission {sub.id} has no submitted_at; set it first")
            continue
        targets.append((sub, post, row, item.line))
    if errors:
        raise ReconcileError("nothing imported:\n  " + "\n  ".join(errors))

    changed = 0
    for sub, post, row, _line in targets:
        did = False
        if sub.state is SubmissionState.AWAITING and row.submitted_at is not None:
            sub.submitted_at = row.submitted_at
            transition(session, sub, SubmissionState.SUBMITTED, actor="operator", detail="reconcile import")
            did = True
        if apply_brand_update(session, sub, row.brand_decision, row.paid_usd, now):
            did = True
        if row.note != sub.note:
            sub.note = row.note
            did = True
        if row.views is not None and row.views != latest_views(session, post.id):
            session.add(ViewSnapshotRow(post_id=post.id, taken_at=now, views=row.views, source="manual"))
            did = True
        changed += int(did)
    session.flush()
    return changed


# --------------------------------------------------------------------------------------------------
# Bot /reconcile walk (state kept in `flags` so a restart does not lose the question)
# --------------------------------------------------------------------------------------------------


def next_open_submission(session: Session, *, skip_ids: frozenset[int] = frozenset()) -> SubmissionRow | None:
    for sub in open_submissions(session):
        if sub.id not in skip_ids:
            return sub
    return None


def reconcile_prompt(session: Session, sub: SubmissionRow, settings: Settings) -> str:
    post = session.get(PostRow, sub.post_id)
    campaign = session.get(CampaignRow, sub.campaign_id)
    assert post is not None
    assert campaign is not None
    views = latest_views(session, post.id)
    submitted = sub.submitted_at.astimezone(settings.app.tz).strftime("%b %d %H:%M") if sub.submitted_at else "?"
    return "\n".join(
        [
            f"Reconcile #{sub.id}: {campaign.name} - {post.platform.value}",
            f"Post: {post.post_url}",
            f"Submitted {submitted}; now {sub.brand_decision.value}; paid ${sub.paid_usd:.2f}"
            + (f"; {views:,} views" if views is not None else ""),
            "What does Content Rewards show?",
        ]
    )


def set_pending_amount(session: Session, submission_id: int, decision: BrandDecision) -> None:
    value = f"{submission_id}:{decision.value}"
    flag = session.get(FlagRow, PENDING_AMOUNT_FLAG)
    if flag is None:
        session.add(FlagRow(key=PENDING_AMOUNT_FLAG, value=value))
    else:
        flag.value = value
    session.flush()


def pending_amount(session: Session) -> tuple[int, BrandDecision] | None:
    flag = session.get(FlagRow, PENDING_AMOUNT_FLAG)
    if flag is None:
        return None
    sub_id, _, decision = flag.value.partition(":")
    try:
        return int(sub_id), BrandDecision(decision)
    except ValueError:
        session.delete(flag)
        return None


def clear_pending_amount(session: Session) -> None:
    flag = session.get(FlagRow, PENDING_AMOUNT_FLAG)
    if flag is not None:
        session.delete(flag)
        session.flush()


def choose_decision(session: Session, submission_id: int, decision: BrandDecision, now: datetime) -> bool:
    """A reconcile button was pressed. Returns True when an amount reply is needed next (Approved / Paid).

    Rejected and Pending are applied at once (a rejection pays nothing); Approved and Paid wait for the amount so the
    operator is asked "reply with $ amount or 0".
    """
    sub = session.get(SubmissionRow, submission_id)
    if sub is None:
        raise ReconcileError(f"submission {submission_id} no longer exists")
    if decision in (BrandDecision.APPROVED, BrandDecision.PAID):
        if sub.state is not SubmissionState.SUBMITTED:
            raise ReconcileError(f"submission {submission_id} is {sub.state.value}, not submitted")
        set_pending_amount(session, submission_id, decision)
        return True
    apply_brand_update(session, sub, decision, 0.0 if decision is BrandDecision.REJECTED else None, now)
    return False


def apply_amount_reply(session: Session, text: str, now: datetime) -> SubmissionRow | None:
    """The operator replied with an amount. Applies the pending decision; returns the submission, or None when no
    question was open. Raises ValueError for text that is not an amount (the question stays open)."""
    pending = pending_amount(session)
    if pending is None:
        return None
    amount = parse_amount(text)
    sub_id, decision = pending
    sub = session.get(SubmissionRow, sub_id)
    if sub is None:
        clear_pending_amount(session)
        return None
    apply_brand_update(session, sub, decision, amount, now)
    clear_pending_amount(session)
    return sub
