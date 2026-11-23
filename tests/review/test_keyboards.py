"""D1: callback-data encoding (pure)."""

from __future__ import annotations

from datetime import date

import pytest

from clipforge.review import keyboards
from clipforge.schemas import RejectReason
from tests.review.fakes import callback_data


def test_roundtrip() -> None:
    data = keyboards.encode(keyboards.REJECT_REASON, 42, "bad_crop")
    assert data == "rr:42:bad_crop"
    assert keyboards.decode(data) == ("rr", ["42", "bad_crop"])


def test_worst_case_ids_fit_in_64_bytes() -> None:
    biggest = [
        keyboards.review_keyboard(10**15),
        keyboards.reject_reason_keyboard(10**15),
        keyboards.arm_keyboard(date(2026, 11, 23)),
        keyboards.submit_keyboard(10**15),
        keyboards.reconcile_keyboard(10**15),
    ]
    for markup in biggest:
        for data in callback_data(markup):
            assert len(data.encode()) <= keyboards.MAX_CALLBACK_BYTES
            keyboards.decode(data)  # everything we emit can be decoded


def test_every_reject_reason_has_a_button() -> None:
    data = callback_data(keyboards.reject_reason_keyboard(7))
    assert {d.split(":")[2] for d in data} == {r.value for r in RejectReason}


def test_review_keyboard_has_the_four_actions() -> None:
    actions = [keyboards.decode(d)[0] for d in callback_data(keyboards.review_keyboard(1))]
    assert actions == [keyboards.APPROVE, keyboards.REJECT, keyboards.NEW_HOOK, keyboards.SHORTER]


@pytest.mark.parametrize("bad", ["", "zz:1", "ap:", "ap::1", "x" * 65, "ap:" + "1" * 70])
def test_decode_refuses_foreign_data(bad: str) -> None:
    with pytest.raises(ValueError, match="bad callback"):
        keyboards.decode(bad)


def test_encode_refuses_unknown_action_and_long_data() -> None:
    with pytest.raises(ValueError, match="unknown"):
        keyboards.encode("nope", 1)
    with pytest.raises(ValueError, match="over 64"):
        keyboards.encode(keyboards.APPROVE, "x" * 70)


def test_int_part() -> None:
    assert keyboards.int_part(["12", "x"]) == 12
    with pytest.raises(ValueError, match="bad callback id"):
        keyboards.int_part(["x"])
    with pytest.raises(ValueError, match="bad callback id"):
        keyboards.int_part([])
