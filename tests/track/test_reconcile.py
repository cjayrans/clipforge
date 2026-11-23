"""D5: reconcile CSV round trip and the bot's /reconcile walk."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from clipforge.config import Settings, load_settings
from clipforge.db import EventRow, PostRow, SubmissionRow, ViewSnapshotRow, make_session_factory
from clipforge.review import bot, keyboards
from clipforge.review.bot import BotDeps
from clipforge.schemas import BrandDecision, Platform, SubmissionState
from clipforge.track.reconcile import (
    CSV_FIELDS,
    ReconcileError,
    apply_amount_reply,
    choose_decision,
    export_csv,
    import_rows,
    next_open_submission,
    open_submissions,
    parse_amount,
)
from tests.review.fakes import FakeNotifier, make_context, make_update
from tests.support.factories import Factory
from tests.track.helpers import T0, real_post, submission, views

NOW = T0 + timedelta(days=3)


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def two_open(factory: Factory, session: Session) -> tuple[SubmissionRow, SubmissionRow]:
    a = submission(factory, session, real_post(factory, session), state=SubmissionState.SUBMITTED)
    b = submission(factory, session, real_post(factory, session, published=T0 + timedelta(hours=1)))
    return a, b  # b is still AWAITING


# --- export ---------------------------------------------------------------------------------------------------------


def test_export_lists_open_real_submissions_only(
    factory: Factory,
    session: Session,
    settings: Settings,
    tmp_path: Path,
    two_open: tuple[SubmissionRow, SubmissionRow],
) -> None:
    a, b = two_open
    post_a = session.get(PostRow, a.post_id)
    assert post_a is not None
    views(session, post_a, 1500, T0 + timedelta(days=1))
    # excluded: missed, rejected, paid, and a dry-run fake URL
    submission(factory, session, real_post(factory, session), state=SubmissionState.MISSED)
    submission(
        factory, session, real_post(factory, session), state=SubmissionState.SUBMITTED, brand=BrandDecision.REJECTED
    )
    submission(
        factory, session, real_post(factory, session), state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID, paid=3
    )
    submission(
        factory,
        session,
        real_post(factory, session, url="https://example.invalid/tiktok/9"),
        state=SubmissionState.SUBMITTED,
    )
    out = tmp_path / "out" / "r.csv"
    assert export_csv(session, settings, out) == 2
    rows = read_rows(out)
    assert [int(r["submission_id"]) for r in rows] == [a.id, b.id]
    assert list(rows[0]) == CSV_FIELDS
    assert rows[0]["views"] == "1500"
    assert rows[0]["brand_decision"] == "pending"
    assert rows[0]["platform"] == "tiktok"
    assert rows[1]["submitted_at"] == ""


# --- import ---------------------------------------------------------------------------------------------------------


def test_round_trip_applies_edits_and_is_idempotent(
    session: Session, settings: Settings, tmp_path: Path, two_open: tuple[SubmissionRow, SubmissionRow]
) -> None:
    a, b = two_open
    path = tmp_path / "r.csv"
    export_csv(session, settings, path)
    rows = read_rows(path)
    rows[0].update(brand_decision="paid", paid_usd="4.50", views="1200", note="paid in round 1")
    rows[1].update(submitted_at="2026-11-01T01:30:00+00:00")
    write_rows(path, rows)
    seen_events = {e.id for e in session.scalars(select(EventRow))}

    assert import_rows(session, settings, path) == 2
    assert a.brand_decision is BrandDecision.PAID
    assert a.paid_usd == 4.5
    assert a.decided_at is not None
    assert a.note == "paid in round 1"
    snap = session.scalars(select(ViewSnapshotRow).where(ViewSnapshotRow.post_id == a.post_id)).one()
    assert (snap.views, snap.source) == (1200, "manual")
    assert b.state is SubmissionState.SUBMITTED
    assert b.submitted_at == datetime(2026, 11, 1, 1, 30, tzinfo=UTC)
    new_events = [e for e in session.scalars(select(EventRow)) if e.id not in seen_events]
    assert new_events
    assert {e.actor for e in new_events} == {"operator"}

    assert import_rows(session, settings, path) == 0  # same file again: nothing changes
    assert len(session.scalars(select(ViewSnapshotRow)).all()) == 1


def test_paid_usd_is_a_total_not_an_increment(
    session: Session, settings: Settings, tmp_path: Path, two_open: tuple[SubmissionRow, SubmissionRow]
) -> None:
    a, _ = two_open
    path = tmp_path / "r.csv"
    export_csv(session, settings, path)
    rows = read_rows(path)
    rows[0].update(brand_decision="approved", paid_usd="2")
    write_rows(path, rows[:1])
    import_rows(session, settings, path)
    import_rows(session, settings, path)
    assert a.paid_usd == 2.0
    rows[0].update(paid_usd="$5.25")
    write_rows(path, rows[:1])
    import_rows(session, settings, path)
    assert a.paid_usd == 5.25


def test_bom_from_excel_is_accepted(
    session: Session, settings: Settings, tmp_path: Path, two_open: tuple[SubmissionRow, SubmissionRow]
) -> None:
    path = tmp_path / "r.csv"
    export_csv(session, settings, path)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    assert import_rows(session, settings, path) == 0


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"submission_id": "9999"}, "no submission 9999"),
        ({"campaign_name": "Other"}, "do not match"),
        ({"post_url": "https://evil.example/x"}, "do not match"),
        ({"platform": "instagram"}, "do not match"),
        ({"brand_decision": "maybe"}, "brand_decision"),
        ({"submitted_at": "2026-11-01T01:30:00"}, "timezone"),
        ({"views": "-4"}, "views"),
        ({"paid_usd": "-1"}, "amount"),
        ({"paid_usd": "abc"}, "amount"),
        ({"brand_decision": "rejected", "paid_usd": "3"}, "rejected submission cannot have a payout"),
    ],
)
def test_bad_rows_are_refused_and_nothing_is_applied(
    session: Session,
    settings: Settings,
    tmp_path: Path,
    two_open: tuple[SubmissionRow, SubmissionRow],
    edit: dict[str, str],
    message: str,
) -> None:
    a, b = two_open
    path = tmp_path / "r.csv"
    export_csv(session, settings, path)
    rows = read_rows(path)
    rows[0].update(brand_decision="paid", paid_usd="9")  # a valid edit that must NOT be applied
    rows[1].update(edit)
    write_rows(path, rows)
    with pytest.raises(ReconcileError, match="nothing imported") as exc:
        import_rows(session, settings, path)
    assert message in exc.value.message
    assert "line 3" in exc.value.message
    assert a.brand_decision is BrandDecision.PENDING
    assert a.paid_usd == 0


def test_missing_columns_and_duplicates_and_missed(
    factory: Factory,
    session: Session,
    settings: Settings,
    tmp_path: Path,
    two_open: tuple[SubmissionRow, SubmissionRow],
) -> None:
    path = tmp_path / "r.csv"
    path.write_text("submission_id,note\n1,x\n", encoding="utf-8")
    with pytest.raises(ReconcileError, match="missing column"):
        import_rows(session, settings, path)

    export_csv(session, settings, path)
    rows = read_rows(path)
    write_rows(path, [rows[0], rows[0]])
    with pytest.raises(ReconcileError, match="appears twice"):
        import_rows(session, settings, path)

    missed = submission(factory, session, real_post(factory, session), state=SubmissionState.MISSED)
    row = dict(rows[0])
    row.update(submission_id=str(missed.id))
    write_rows(path, [row])
    with pytest.raises(ReconcileError, match="(do not match|missed)"):
        import_rows(session, settings, path)


def test_brand_decision_needs_a_submitted_submission(
    session: Session, settings: Settings, tmp_path: Path, two_open: tuple[SubmissionRow, SubmissionRow]
) -> None:
    path = tmp_path / "r.csv"
    export_csv(session, settings, path)
    rows = read_rows(path)
    rows[1].update(brand_decision="approved")  # row 1 was never submitted and has no submitted_at
    write_rows(path, rows)
    with pytest.raises(ReconcileError, match="no submitted_at"):
        import_rows(session, settings, path)


# --- amounts --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [("12.50", 12.5), ("$12.50", 12.5), (" $ 1,234.5 ", 1234.5), ("0", 0.0), (".5", 0.5), ("7", 7.0)],
)
def test_parse_amount(text: str, value: float) -> None:
    assert parse_amount(text) == value


@pytest.mark.parametrize("text", ["", "abc", "-3", "1e5", "nan", "inf", "12.5.1", "$", "1 2"])
def test_parse_amount_refuses(text: str) -> None:
    with pytest.raises(ValueError, match="amount"):
        parse_amount(text)


# --- the /reconcile walk (service level) ------------------------------------------------------------------------------


def test_walk_is_oldest_first_and_skips_closed(factory: Factory, session: Session) -> None:
    late = submission(
        factory,
        session,
        real_post(factory, session, published=T0 + timedelta(hours=5)),
        state=SubmissionState.SUBMITTED,
    )
    early = submission(factory, session, real_post(factory, session, published=T0), state=SubmissionState.SUBMITTED)
    submission(factory, session, real_post(factory, session), state=SubmissionState.SUBMITTED, brand=BrandDecision.PAID)
    submission(factory, session, real_post(factory, session))  # awaiting: not part of the walk
    assert [s.id for s in open_submissions(session)] == [early.id, late.id]
    assert next_open_submission(session) is early
    assert next_open_submission(session, skip_ids=frozenset({early.id})) is late


def test_rejected_and_pending_apply_at_once_approved_and_paid_ask_for_an_amount(
    factory: Factory, session: Session
) -> None:
    sub = submission(factory, session, real_post(factory, session), state=SubmissionState.SUBMITTED)
    assert not choose_decision(session, sub.id, BrandDecision.REJECTED, NOW)
    assert sub.brand_decision is BrandDecision.REJECTED
    assert sub.paid_usd == 0
    other = submission(factory, session, real_post(factory, session), state=SubmissionState.SUBMITTED)
    assert choose_decision(session, other.id, BrandDecision.APPROVED, NOW)
    assert other.brand_decision is BrandDecision.PENDING  # not applied until the amount arrives
    with pytest.raises(ValueError, match="amount"):
        apply_amount_reply(session, "lots", NOW)
    done = apply_amount_reply(session, "$12.50", NOW)
    assert done is other
    assert (other.brand_decision, other.paid_usd) == (BrandDecision.APPROVED, 12.5)
    assert apply_amount_reply(session, "5", NOW) is None  # no open question any more


def test_cannot_reconcile_an_unsubmitted_submission(factory: Factory, session: Session) -> None:
    sub = submission(factory, session, real_post(factory, session))
    with pytest.raises(ReconcileError, match="not submitted"):
        choose_decision(session, sub.id, BrandDecision.PAID, NOW)


# --- the /reconcile walk (bot handlers) ---------------------------------------------------------


@pytest.fixture
def deps(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> BotDeps:
    monkeypatch.setenv("TELEGRAM_OWNER_CHAT_ID", "111")
    return BotDeps(
        settings=load_settings(), factory=make_session_factory(engine), notifier=FakeNotifier(), clock=lambda: NOW
    )


async def test_reconcile_conversation(deps: BotDeps, factory: Factory, session: Session) -> None:
    first = submission(factory, session, real_post(factory, session, published=T0), state=SubmissionState.SUBMITTED)
    second = submission(
        factory,
        session,
        real_post(factory, session, published=T0 + timedelta(hours=1), platform=Platform.INSTAGRAM),
        state=SubmissionState.SUBMITTED,
    )
    session.commit()
    ctx = make_context(deps)

    start = make_update(111)
    await bot.cmd_reconcile(start, ctx)
    text = start.effective_message.reply_text.await_args.args[0]
    assert f"Reconcile #{first.id}" in text
    kb = start.effective_message.reply_text.await_args.kwargs["reply_markup"]
    assert [b.callback_data for b in kb.inline_keyboard[0]] == [
        f"rc:{first.id}:{d}" for d in ("approved", "rejected", "pending", "paid")
    ]

    press = make_update(111, keyboards.encode(keyboards.RECONCILE, first.id, "paid"))
    await bot.on_callback(press, ctx)
    assert "total $ amount" in press.callback_query.message.reply_text.await_args.args[0]

    bad = make_update(111)
    bad.effective_message.text = "lots"
    await bot.on_text(bad, ctx)
    assert "number" in bad.effective_message.reply_text.await_args.args[0]

    amount = make_update(111)
    amount.effective_message.text = "$7.25"
    await bot.on_text(amount, ctx)
    reply = amount.effective_message.reply_text.await_args.args[0]
    assert f"Saved: #{first.id} paid, paid $7.25." in reply
    assert f"Reconcile #{second.id}" in reply  # moved on to the next submission

    reject = make_update(111, keyboards.encode(keyboards.RECONCILE, second.id, "rejected"))
    await bot.on_callback(reject, ctx)
    assert "Nothing left to reconcile." in reject.callback_query.message.reply_text.await_args.args[0]
    session.expire_all()
    assert session.get(SubmissionRow, first.id).paid_usd == 7.25  # type: ignore[union-attr]
    assert session.get(SubmissionRow, second.id).brand_decision is BrandDecision.REJECTED  # type: ignore[union-attr]


async def test_chatter_without_an_open_question_is_ignored(deps: BotDeps) -> None:
    update = make_update(111)
    update.effective_message.text = "hello"
    await bot.on_text(update, make_context(deps))
    update.effective_message.reply_text.assert_not_awaited()


async def test_stranger_text_is_ignored(deps: BotDeps) -> None:
    update = make_update(222)
    update.effective_message.text = "5"
    await bot.on_text(update, make_context(deps))
    update.effective_message.reply_text.assert_not_awaited()
