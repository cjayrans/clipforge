"""B5: eval harness. Replay mode end to end, metrics, and the live-mode guard (no network in tests)."""

from __future__ import annotations

import json
from pathlib import Path

import anthropic
import pytest
from sqlalchemy import select

from clipforge import costs
from clipforge.db import CostRow, session_scope
from clipforge.rank.score import TOOL_NAME
from evals import run as ev
from tests.rank.anthropic_mock import FakeAnthropic, tool_use
from tests.rank.test_score import echo_scores


def run_main(tmp_path: Path, *extra: str) -> tuple[int, dict]:
    out = tmp_path / "report.json"
    code = ev.main(["--out", str(out), *extra])
    return code, (json.loads(out.read_text()) if out.exists() else {})


def test_replay_run_reports_validity_and_label_overlap(tmp_path):
    code, report = run_main(tmp_path)
    assert code == 0
    assert report["mode"] == "replay"
    assert report["month_to_date_usd"] == 0.0
    by_name = {t["transcript"]: t for t in report["transcripts"]}
    assert set(by_name) == {"transcript_talk", "transcript_sparse"}
    talk, sparse = by_name["transcript_talk"], by_name["transcript_sparse"]
    assert talk["validity"] == 1.0
    assert sparse["validity"] == 1.0
    assert talk["invalid_items"] == 0
    assert talk["windows"] > 0
    assert talk["calls"] == -(-talk["windows"] // 10)
    # Replay data scores the billboard span highest among non-overlapping candidates: one of two good labels hit.
    assert talk["metrics"]["recall_at_k"] == 0.5
    assert talk["metrics"]["bad_at_k"] == 0
    assert sparse["metrics"]["recall_at_k"] is None  # no good labels
    assert sparse["metrics"]["bad_at_k"] == 1
    assert all(t["score"] <= 100 for t in talk["top"])


def test_replay_is_deterministic(tmp_path):
    _, a = run_main(tmp_path / "a")
    _, b = run_main(tmp_path / "b")
    assert [t["top"] for t in a["transcripts"]] == [t["top"] for t in b["transcripts"]]


def test_malformed_replay_items_counted_invalid(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    data = json.loads((ev.DEFAULT_REPLAY_DIR / "transcript_talk.json").read_text())
    data["spans"][0]["score"] = 150  # out of range for schemas.WindowScore
    (replay / "transcript_talk.json").write_text(json.dumps(data))
    code, report = run_main(tmp_path, "--replay-dir", str(replay), "--transcripts", str(ev.DEFAULT_TRANSCRIPTS[0]))
    assert code == 0
    talk = report["transcripts"][0]
    assert talk["invalid_items"] > 0
    assert talk["validity"] < 1.0


def test_live_without_key_sends_nothing(tmp_path, monkeypatch, capsys):
    def boom(*a, **kw):
        raise AssertionError("no Anthropic client may be built without a key")

    monkeypatch.setattr(anthropic, "Anthropic", boom)
    code, report = run_main(tmp_path, "--live")
    assert code == 2
    assert report == {}
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_live_without_database_refuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: pytest.fail("must not build a client"))
    code, _ = run_main(tmp_path, "--live")
    assert code == 2
    assert "cf db upgrade" in capsys.readouterr().out


def test_bad_args():
    with pytest.raises(SystemExit):
        ev.parse_args(["--k", "0"])


def test_stability_across_repeats(client, fake: FakeAnthropic):
    fake.default = echo_scores
    reports = ev.evaluate(
        transcripts=[ev.DEFAULT_TRANSCRIPTS[0]],
        campaign=ev.CampaignIn.model_validate(json.loads(ev.DEFAULT_CAMPAIGN.read_text())),
        labels=ev.load_labels(ev.DEFAULT_LABELS),
        client=client,
        k=3,
        repeat=2,
        iou_threshold=0.5,
    )
    assert len(reports[0].runs) == 2
    assert reports[0].stability == 1.0  # identical answers both times


def test_iou_and_jaccard():
    assert ev.iou((0, 10), (0, 10)) == 1.0
    assert ev.iou((0, 10), (5, 15)) == pytest.approx(5 / 15)
    assert ev.iou((0, 10), (20, 30)) == 0.0
    assert ev.jaccard([], []) == 1.0
    assert ev.jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


def test_topk_metrics():
    labels = [
        ev.Label("t", 0, 30, "good"),
        ev.Label("t", 100, 130, "good"),
        ev.Label("t", 200, 230, "bad"),
    ]
    m = ev.topk_metrics([(0, 30), (200, 228), (400, 430)], labels, k=3, thr=0.5)
    assert m["precision_at_k"] == pytest.approx(1 / 3)
    assert m["recall_at_k"] == 0.5
    assert m["bad_at_k"] == 1
    empty = ev.topk_metrics([], [], k=3, thr=0.5)
    assert empty["precision_at_k"] is None
    assert empty["recall_at_k"] is None


def test_load_labels(tmp_path):
    assert ev.load_labels(tmp_path / "missing.csv") == []
    seeded = ev.load_labels(ev.DEFAULT_LABELS)
    assert {lb.label for lb in seeded} == {"good", "bad"}
    bad = tmp_path / "bad.csv"
    bad.write_text("transcript,start_s,end_s,label,note\nt,0,30,meh,\n")
    with pytest.raises(ValueError, match="good or bad"):
        ev.load_labels(bad)


def test_cap_stop_keeps_spend_and_blocks_next_run(client, fake: FakeAnthropic, ledger, app_config):
    """COMPLIANCE #12 regression: a run stopped by the cap keeps its cost rows; the next run sends nothing."""

    def expensive(body):  # $1.00 per call at the example config's Haiku output price
        scores = json.loads(echo_scores(body).content)["content"][0]["input"]
        return tool_use(TOOL_NAME, scores, input_tokens=0, output_tokens=2_000_000)

    fake.default = expensive
    args = {
        "transcripts": [ev.DEFAULT_TRANSCRIPTS[0]],
        "campaign": ev.CampaignIn.model_validate(json.loads(ev.DEFAULT_CAMPAIGN.read_text())),
        "labels": [],
        "client": client,
        "k": 3,
        "repeat": 50,
        "iou_threshold": 0.5,
    }
    # The caller's own transaction rolls back when the cap stops the run.
    with pytest.raises(costs.SpendCapReached), session_scope(ledger):
        ev.evaluate(**args)
    sent = len(fake.requests)
    assert sent == int(app_config.monthly_api_cap_usd)  # $15 cap, $1 per call

    with ledger() as fresh:
        assert len(fresh.scalars(select(CostRow)).all()) == 2 * sent
        assert costs.month_to_date(fresh, app_config) >= app_config.monthly_api_cap_usd

    with pytest.raises(costs.SpendCapReached):
        ev.evaluate(**args)
    assert len(fake.requests) == sent  # refused before any request
