"""Secret redaction in logs."""

from __future__ import annotations

import logging
import sys

import pytest

from clipforge.logsetup import REDACTED, RedactingFilter, setup_logging

SECRET = "super-secret-value-123"


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


def test_configured_secret_redacted_in_message_and_args() -> None:
    f = RedactingFilter([SECRET])
    rec = _record("calling with %s and %s", SECRET, "ok")
    f.filter(rec)
    assert SECRET not in rec.getMessage()
    assert REDACTED in rec.getMessage()
    assert "ok" in rec.getMessage()


@pytest.mark.parametrize(
    "text",
    [
        "key sk-ant-api03-abcdefghijklmnop",
        "groq gsk_ABCDEFGHIJKLMNOPQRSTUV",
        "bot 123456789:AAEhBP0av28GSM9fGq6gCLkiiFf1kOmuQ3k",
        "Authorization: Bearer abc.def.ghi",
        "headers x-api-key=xyz123",
    ],
)
def test_token_shapes_redacted(text: str) -> None:
    out = RedactingFilter().redact(text)
    assert REDACTED in out
    for secret in ("abcdefghijklmnop", "ABCDEFGHIJKLMNOP", "AAEhBP0av28", "abc.def.ghi", "xyz123"):
        assert secret not in out


def test_short_values_ignored() -> None:
    assert RedactingFilter(["ab"]).redact("ab cd") == "ab cd"


def test_exception_text_redacted() -> None:
    flt = setup_logging([SECRET])
    try:
        raise RuntimeError(f"failed with token {SECRET}")
    except RuntimeError:
        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "boom", None, sys.exc_info())
    flt.filter(rec)
    assert rec.exc_text is not None
    assert SECRET not in rec.exc_text


def test_setup_logging_is_idempotent() -> None:
    setup_logging()
    setup_logging()
    marked = [h for h in logging.getLogger().handlers if getattr(h, "_clipforge", False)]
    assert len(marked) == 1
