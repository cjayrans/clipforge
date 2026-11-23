"""`cf` CLI skeleton: Phase 0 commands work; later-phase commands name their owner."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from clipforge import cli, costs
from clipforge.config import load_settings
from clipforge.db import make_engine, make_session_factory, session_scope

runner = CliRunner()
REPO = Path(__file__).resolve().parent.parent


def _run(*args: str) -> tuple[int, str]:
    result = runner.invoke(cli.app, list(args))
    return result.exit_code, result.output


def test_help_lists_every_command() -> None:
    code, out = _run("--help")
    assert code == 0
    for name in (
        "campaign",
        "source",
        "status",
        "costs",
        "resume",
        "worker",
        "bot",
        "run",
        "report",
        "export-reconcile",
        "import-reconcile",
        "backup",
        "db",
    ):
        assert name in out


@pytest.mark.parametrize(
    ("args", "owner"),
    [
        (["run", "--once"], "Phase 2"),
    ],
)
def test_later_phase_commands_name_their_owner(args: list[str], owner: str) -> None:
    code, out = _run(*args)
    assert code == cli.EXIT_NOT_IMPLEMENTED
    assert owner in out
    assert "clipforge." in out


def test_every_target_is_known_to_be_missing_now() -> None:
    # Phase 1 A implemented ingest.* (tests/ingest/test_cli_campaign.py); Phase 1 D implemented the rest of the
    # Phase 1 targets (tests/ops/test_cli_targets.py).
    for command, target in cli.TARGETS.items():
        if target.module.startswith("ingest.") or "Phase 1 D" in target.owner:
            continue
        with pytest.raises(cli.NotImplementedYet):
            cli.resolve(command)


def test_status_and_costs_on_fresh_db() -> None:
    code, out = _run("status")
    assert code == 0, out
    assert "dry_run" in out
    assert "running" in out
    assert "never" in out
    code, out = _run("costs")
    assert code == 0, out
    assert "$0.00 of $15.00" in out


def test_resume_clears_pause() -> None:
    _run("db", "upgrade")
    settings = load_settings()
    engine = make_engine(settings.database_url)
    with session_scope(make_session_factory(engine)) as s:
        costs.pause(s, "spend cap")
    code, out = _run("resume")
    assert code == 0
    assert "resumed" in out
    code, out = _run("resume")
    assert "not paused" in out
    with session_scope(make_session_factory(engine)) as s:
        assert costs.is_paused(s) is None
    engine.dispose()


def test_backup_writes_file() -> None:
    code, out = _run("backup")
    assert code == 0, out
    assert Path(out.strip().splitlines()[-1]).exists()


def test_bad_config_exits_cleanly(tmp_path: Path) -> None:
    code, out = _run("--config", str(tmp_path / "missing.yaml"), "status")
    assert code == cli.EXIT_CONFIG
    assert "not found" in out


def test_run_without_once() -> None:
    code, _ = _run("run")
    assert code == cli.EXIT_CONFIG
