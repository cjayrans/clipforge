"""`cf campaign ...` / `cf source add` end to end through the real CLI (temp data dir from conftest)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from typer.testing import CliRunner

from clipforge import cli
from clipforge.config import load_settings
from clipforge.db import EventRow, SourceRow, make_engine, make_session_factory, session_scope

runner = CliRunner()
REPO = Path(__file__).resolve().parents[2]
ASIANJEFF = REPO / "campaigns" / "asianjeff-gta6.json"


def _run(*args: str) -> tuple[int, str]:
    result = runner.invoke(cli.app, list(args))
    return result.exit_code, result.output + result.stderr


def test_campaign_commands_end_to_end() -> None:
    code, out = _run("campaign", "add", "--from-json", str(ASIANJEFF))
    assert code == 0, out
    assert "registered campaign 1" in out
    assert "inbox/asianjeff-clipping-reach/" in out

    code, out = _run("campaign", "list")
    assert code == 0, out
    assert "AsianJeff" in out

    code, out = _run("campaign", "check", "1")
    assert code == 0, out
    assert "footage_mode" in out

    code, out = _run("source", "add", "1", "https://cdn.example.com/ep1.mp4")
    assert code == 0, out
    assert "registered: source 1" in out

    code, out = _run("source", "add", "1", "https://www.youtube.com/watch?v=abc")
    assert code == 0, out
    assert "refused" in out

    code, out = _run("campaign", "drain", "1")
    assert code == 0, out
    assert "active -> drained" in out
    code, out = _run("campaign", "end", "1")
    assert code == 0, out
    code, out = _run("campaign", "drain", "1")
    assert code == 1
    assert "Error" in out
    assert "cannot become" in out

    # The refusal alert was committed with the command.
    engine = make_engine(load_settings().database_url)
    with session_scope(make_session_factory(engine)) as s:
        alerts = list(s.scalars(select(EventRow).where(EventRow.entity == "alert")))
        assert [a.to_state for a in alerts] == ["host_refused"]
        assert [r.url for r in s.scalars(select(SourceRow))] == ["https://cdn.example.com/ep1.mp4"]
    engine.dispose()


def test_unknown_campaign_is_a_clean_error() -> None:
    code, out = _run("campaign", "check", "42")
    assert code == 1
    assert "no campaign with id 42" in out
