"""`cf` CLI skeleton: Phase 0 commands work; later-phase commands name their owner."""

from __future__ import annotations

from pathlib import Path

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
        "post",
        "clip",
    ):
        assert name in out


def test_every_target_resolves() -> None:
    """Phase 2: every `cf` command is implemented (P2.6); `resolve` never raises NotImplementedYet any more."""
    for command in cli.TARGETS:
        assert callable(cli.resolve(command)), command


def test_run_once_on_empty_db_prints_a_summary() -> None:
    """`cf run --once` with nothing registered: no paid call, a summary, exit 0."""
    code, out = _run("run", "--once")
    assert code == 0, out
    assert "nightly run: 0 clip(s) ready for review" in out


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


def test_post_repair_is_a_dry_run_unless_told_otherwise_and_exits_1_when_it_cannot_repair() -> None:
    """No post 987654 exists: refused, nothing changed, no HTTP (a missing post never reaches Upload-Post)."""
    code, out = _run("post", "repair", "987654")
    assert code == 1
    assert "post 987654" in out
    assert "no such post" in out
    assert "dry run: nothing changed" in out
    code, out = _run("post", "repair", "987654", "--apply")
    assert code == 1
    assert "dry run" not in out


def test_clip_relayout_needs_ids_or_all_pending() -> None:
    code, out = _run("clip", "relayout")
    assert code == cli.EXIT_CONFIG
    assert "--all-pending" in out


def test_clip_relayout_dry_run_on_empty_db_changes_nothing() -> None:
    code, out = _run("clip", "relayout", "--all-pending", "--dry-run")
    assert code == 0
    assert "dry run: 0 of 0 clip(s) would be re-rendered" in out


def test_clip_relayout_refuses_an_unknown_clip() -> None:
    code, out = _run("clip", "relayout", "987654")
    assert code == cli.EXIT_CHECK_FAILED
    assert "clip 987654: not re-rendered" in out


def test_source_facecam_unknown_source() -> None:
    code, out = _run("source", "facecam", "987654")
    assert code != 0
    assert "no source with id 987654" in out
