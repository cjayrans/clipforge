"""Every `cf` command owned by Phase 1 D resolves to a real function with the documented name, and the data commands
work end to end against a temporary database.

(tests/test_cli.py asserts the *not yet implemented* message for other phases; these commands must not be listed there,
because invoking `cf worker` or `cf bot` for real would start a long-running process.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from clipforge import cli
from tests.track.test_reconcile import CSV_FIELDS, read_rows, write_rows

OPS_TARGETS = {
    "worker": "clipforge.scheduler.worker.run_worker",
    "bot": "clipforge.review.bot.run_bot",
    "report weekly": "clipforge.track.reports.weekly_report",
    "export-reconcile": "clipforge.track.reconcile.export_csv",
    "import-reconcile": "clipforge.track.reconcile.import_csv",
}

runner = CliRunner()


@pytest.mark.parametrize(("command", "dotted"), sorted(OPS_TARGETS.items()))
def test_ops_command_resolves(command: str, dotted: str) -> None:
    fn = cli.resolve(command)
    assert f"{fn.__module__}.{fn.__name__}" == dotted
    assert cli.TARGETS[command].dotted == dotted


def test_report_weekly_writes_and_prints_the_path() -> None:
    result = runner.invoke(cli.app, ["report", "weekly"])
    assert result.exit_code == 0, result.output
    path = Path(result.output.strip())
    assert path.name.startswith("weekly-")
    assert path.suffix == ".md"
    assert "# Weekly digest" in path.read_text(encoding="utf-8")


def test_export_then_import_on_an_empty_database(tmp_path: Path) -> None:
    out = tmp_path / "r.csv"
    result = runner.invoke(cli.app, ["export-reconcile", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert read_rows(out) == []
    assert out.read_text(encoding="utf-8").splitlines()[0] == ",".join(CSV_FIELDS)
    result = runner.invoke(cli.app, ["import-reconcile", str(out)])
    assert result.exit_code == 0, result.output


def test_import_reports_bad_files_as_a_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    write_rows(
        bad,
        [
            {
                "submission_id": "42",
                "campaign_name": "x",
                "platform": "tiktok",
                "post_url": "https://x.example/1",
                "submitted_at": "",
                "brand_decision": "pending",
                "views": "",
                "paid_usd": "",
                "note": "",
            }
        ],
    )
    result = runner.invoke(cli.app, ["import-reconcile", str(bad)])
    assert result.exit_code == 1
    assert "nothing imported" in result.output
    assert "no submission 42" in result.output
    assert "Traceback" not in result.output
