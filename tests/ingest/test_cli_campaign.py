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


def test_campaign_check_exits_1_when_a_check_fails(tmp_path: Path) -> None:
    """CR-A3: `cf campaign check` exits non-zero when the campaign fails the filter."""
    import json

    data = json.loads(ASIANJEFF.read_text())
    data["footage_mode"] = "own_gameplay"  # never allowed by campaign_filter.allowed_footage_modes
    data["name"] = "Own Gameplay Campaign"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(data))
    code, out = _run("campaign", "add", "--from-json", str(bad))
    assert code == 0, out
    code, out = _run("campaign", "check", "1")
    assert code == cli.EXIT_CHECK_FAILED, out
    assert "FAIL" in out


def test_source_retry_end_to_end() -> None:
    """CR-A5: `cf source retry` puts a FAILED source back to REGISTERED with an events row."""
    from clipforge.ingest.models import set_source_status
    from clipforge.schemas import SourceStatus

    assert _run("campaign", "add", "--from-json", str(ASIANJEFF))[0] == 0
    assert _run("source", "add", "1", "https://cdn.example.com/ep1.mp4")[0] == 0
    code, out = _run("source", "retry", "1")
    assert code == 1
    assert "only failed sources" in out

    engine = make_engine(load_settings().database_url)
    fac = make_session_factory(engine)
    with session_scope(fac) as s:
        src = s.get(SourceRow, 1)
        assert src is not None
        set_source_status(s, src, SourceStatus.FAILED, fail_reason="download_failed")
    code, out = _run("source", "retry", "1")
    assert code == 0, out
    assert "-> registered" in out
    with session_scope(fac) as s:
        src = s.get(SourceRow, 1)
        assert src is not None
        assert src.status is SourceStatus.REGISTERED
        assert src.fail_reason is None
        last = s.scalars(select(EventRow).where(EventRow.entity == "source").order_by(EventRow.id.desc())).first()
        assert last is not None
        assert last.actor == "operator"
        assert "retry after download_failed" in last.detail
    assert _run("source", "retry", "99")[0] == 1
    engine.dispose()


def test_source_rerank_end_to_end() -> None:
    """`cf source rerank` refuses a source that is not ranked, then puts a ranked one back to TRANSCRIBED."""
    from clipforge.db import TranscriptRow
    from clipforge.ingest.models import set_source_status
    from clipforge.schemas import SourceStatus

    assert _run("campaign", "add", "--from-json", str(ASIANJEFF))[0] == 0
    assert _run("source", "add", "1", "https://cdn.example.com/ep1.mp4")[0] == 0
    code, out = _run("source", "rerank", "1")
    assert code == 1
    assert "only ranked sources" in out

    engine = make_engine(load_settings().database_url)
    fac = make_session_factory(engine)
    with session_scope(fac) as s:
        src = s.get(SourceRow, 1)
        assert src is not None
        s.add(TranscriptRow(source_id=1, provider="groq", model="m", words_json_path="/x.json"))
        set_source_status(s, src, SourceStatus.RANKED)
    code, out = _run("source", "rerank", "1")
    assert code == 0, out
    assert "ranked -> transcribed" in out
    with session_scope(fac) as s:
        src = s.get(SourceRow, 1)
        assert src is not None
        assert src.status is SourceStatus.TRANSCRIBED
    assert _run("source", "rerank", "99")[0] == 1
    engine.dispose()
