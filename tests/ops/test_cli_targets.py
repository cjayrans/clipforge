"""Every `cf` command owned by Phase 1 D resolves to a real function with the documented name.

(tests/test_cli.py asserts the *not yet implemented* message for other phases; these commands must not be listed there,
because invoking `cf worker` or `cf bot` for real would start a long-running process.)
"""

from __future__ import annotations

import pytest

from clipforge import cli

OPS_TARGETS = {
    "bot": "clipforge.review.bot.run_bot",
}


@pytest.mark.parametrize(("command", "dotted"), sorted(OPS_TARGETS.items()))
def test_ops_command_resolves(command: str, dotted: str) -> None:
    fn = cli.resolve(command)
    assert f"{fn.__module__}.{fn.__name__}" == dotted
    assert cli.TARGETS[command].dotted == dotted
