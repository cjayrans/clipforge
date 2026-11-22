"""Session-level guards for the test suite."""

from __future__ import annotations

from collections.abc import Mapping


class LivePublishModeError(RuntimeError):
    pass


def refuse_live_publish_mode(environ: Mapping[str, str]) -> None:
    """Tests never run with live publishing (docs/COMPLIANCE.md). Raise if the environment asks for it."""
    mode = environ.get("PUBLISH_MODE", "").strip().lower()
    if mode == "live":
        raise LivePublishModeError("PUBLISH_MODE is set to live: refusing to run tests. Unset it (tests use dry_run).")
