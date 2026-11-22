"""Logging with secret redaction (ARCHITECTURE §8, PRD §6).

`setup_logging(secrets)` installs a root handler whose `RedactingFilter` replaces every configured secret value and
anything shaped like an API key or bot token with `***`, in the message, its args and exception text.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable

REDACTED = "***"

# Token shapes redacted even when not configured (e.g. a key pasted into an error message).
TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic
    re.compile(r"gsk_[A-Za-z0-9]{16,}"),  # Groq
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # Telegram bot token
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"(?i)(authorization|apikey|api_key|x-api-key)(\s*[:=]\s*)(bearer\s+|apikey\s+)?[^\s,;'\"]+"),
)
MIN_SECRET_LEN = 6


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        values = sorted({s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True)
        self._literal = re.compile("|".join(re.escape(v) for v in values)) if values else None

    def redact(self, text: str) -> str:
        if self._literal is not None:
            text = self._literal.sub(REDACTED, text)
        for pattern in TOKEN_PATTERNS:
            if pattern.groups >= 2:
                text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
            else:
                text = pattern.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_info:
            formatter = logging.Formatter()
            record.exc_text = self.redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def setup_logging(secrets: Iterable[str] = (), *, level: int = logging.INFO) -> RedactingFilter:
    """Configure the root logger once per process. Returns the filter (tests use it)."""
    flt = RedactingFilter(secrets)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(flt)
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_clipforge", False):
            root.removeHandler(h)
    handler._clipforge = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it at WARNING so query-string keys never reach the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return flt
