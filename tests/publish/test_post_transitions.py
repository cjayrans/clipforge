"""Who may move a post into PUBLISHING (FAILED -> PUBLISHING exists only for `cf post repair`; invariant 7)."""

from __future__ import annotations

import re
from pathlib import Path

from clipforge import schemas

SRC = Path(__file__).resolve().parents[2] / "src" / "clipforge"
ALLOWED = {"publish/dispatcher.py", "publish/repair.py"}


def test_failed_posts_can_only_go_back_to_publishing() -> None:
    assert schemas.POST_TRANSITIONS[schemas.PostState.FAILED] == {schemas.PostState.PUBLISHING}
    assert schemas.POST_TRANSITIONS[schemas.PostState.LIVE] == frozenset()


def test_only_the_dispatcher_and_the_repair_move_posts_into_publishing() -> None:
    pattern = re.compile(r"transition\([^)]*PostState\.PUBLISHING", re.DOTALL)
    users = {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if pattern.search(path.read_text(encoding="utf-8"))
    }
    assert users == ALLOWED, users


def test_repair_records_the_operator_and_never_sends() -> None:
    """The dispatcher only moves a fresh post (QUEUED) to PUBLISHING; repair is the FAILED -> PUBLISHING path."""
    repair = (SRC / "publish" / "repair.py").read_text(encoding="utf-8")
    assert 'actor="operator"' in repair
    assert ".publish(" not in repair
