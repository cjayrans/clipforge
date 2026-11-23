"""Inline keyboards and callback-data encoding (pure).

Callback data is `<action>:<part>[:<part>...]` and must be at most 64 bytes (Telegram's limit for
InlineKeyboardButton.callback_data; per docs/ARCHITECTURE.md §4.6). Every id in it is an integer or an ISO date.
"""

from __future__ import annotations

from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from clipforge.schemas import BrandDecision, RejectReason

MAX_CALLBACK_BYTES = 64

APPROVE = "ap"
REJECT = "rj"  # open the reason menu
REJECT_REASON = "rr"
NEW_HOOK = "nh"
SHORTER = "sh"
ARM = "arm"
SKIP = "skp"
SUBMITTED = "sub"
MISSED = "mis"
RECONCILE = "rc"

ACTIONS = frozenset({APPROVE, REJECT, REJECT_REASON, NEW_HOOK, SHORTER, ARM, SKIP, SUBMITTED, MISSED, RECONCILE})

REASON_LABELS: dict[RejectReason, str] = {
    RejectReason.OFF_BRIEF: "Off-brief",
    RejectReason.BORING: "Boring",
    RejectReason.BAD_CROP: "Bad crop",
    RejectReason.BAD_CAPTIONS: "Bad captions",
    RejectReason.OTHER: "Other",
}


def encode(action: str, *parts: object) -> str:
    if action not in ACTIONS:
        raise ValueError(f"unknown callback action {action!r}")
    data = ":".join([action, *(str(p) for p in parts)])
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback data over {MAX_CALLBACK_BYTES} bytes: {data!r}")
    return data


def decode(data: str) -> tuple[str, list[str]]:
    """Split callback data into (action, parts). Raises ValueError for anything we did not produce."""
    if not data or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("bad callback data")
    action, *parts = data.split(":")
    if action not in ACTIONS or any(not p for p in parts):
        raise ValueError("bad callback data")
    return action, parts


def int_part(parts: list[str], index: int = 0) -> int:
    try:
        return int(parts[index])
    except (IndexError, ValueError) as exc:
        raise ValueError("bad callback id") from exc


def _btn(label: str, action: str, *parts: object) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=encode(action, *parts))


def review_keyboard(clip_id: int, stamp: int) -> InlineKeyboardMarkup:
    """Approve / Reject carry the render stamp, so a button on an old preview cannot act on a newer render."""
    return InlineKeyboardMarkup(
        [
            [_btn("Approve", APPROVE, clip_id, stamp), _btn("Reject", REJECT, clip_id, stamp)],
            [_btn("New hook", NEW_HOOK, clip_id), _btn("Shorter", SHORTER, clip_id)],
        ]
    )


def reject_reason_keyboard(clip_id: int, stamp: int) -> InlineKeyboardMarkup:
    buttons = [_btn(label, REJECT_REASON, clip_id, stamp, reason.value) for reason, label in REASON_LABELS.items()]
    return InlineKeyboardMarkup([buttons[:3], buttons[3:]])


def arm_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[_btn("Arm tonight", ARM, day.isoformat()), _btn("Skip tonight", SKIP, day.isoformat())]]
    )


def submit_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[_btn("Submitted", SUBMITTED, submission_id), _btn("Missed", MISSED, submission_id)]])


def reconcile_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    labels = [
        ("Approved", BrandDecision.APPROVED),
        ("Rejected", BrandDecision.REJECTED),
        ("Pending", BrandDecision.PENDING),
        ("Paid", BrandDecision.PAID),
    ]
    return InlineKeyboardMarkup([[_btn(label, RECONCILE, submission_id, d.value) for label, d in labels]])
