"""B3: Haiku scorer: batching, forced tool, schema validation, cost recording, skipped batches."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from clipforge.db import CostRow
from clipforge.rank.prompts import Fewshot
from clipforge.rank.score import BATCH_SIZE, TOOL_NAME, batches, parse_scores, score_tool_schema, score_windows
from clipforge.rank.windows import build_windows
from clipforge.schemas import Window
from tests.rank.anthropic_mock import FakeAnthropic, message, tool_use
from tests.support.factories import campaign_in


def make_windows(n: int) -> list[Window]:
    return [
        Window(window_id=f"9:{i}", source_id=9, start_s=i * 10.0, end_s=i * 10.0 + 30.0, text=f"window {i}")
        for i in range(n)
    ]


def item(window_id: str, score: int = 50, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "window_id": window_id,
        "score": score,
        "standalone": True,
        "brief_ok": True,
        "hook_potential": 5,
        "trim_start_s": None,
        "trim_end_s": None,
        "reasons": "ok",
    }
    data.update(kw)
    return data


def echo_scores(body: dict[str, Any]):
    """Score every window id found in the request (score = 10 + index within the batch)."""
    text = body["messages"][0]["content"]
    ids = [part.split('"', 1)[0] for part in text.split('<window id="')[1:]]
    return tool_use(TOOL_NAME, {"scores": [item(i, 10 + k) for k, i in enumerate(ids)]})


def test_schema_is_strict_compatible():
    schema = score_tool_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for banned in ("minimum", "maximum", "minLength", "maxLength", "maxItems", "oneOf"):
                assert banned not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


def test_batches():
    assert [len(b) for b in batches(make_windows(25))] == [10, 10, 5]
    assert batches([]) == []


def test_parse_scores_validates_and_filters():
    raw = {
        "scores": [
            item("a", 80),
            item("a", 70),  # duplicate
            item("zzz"),  # not requested
            item("b", 101),  # out of range
            item("c", hook_potential=11),
            {"window_id": "d"},  # missing fields
            "junk",
            item("e", reasons="x" * 900),  # shortened, still valid
        ]
    }
    valid, returned, invalid = parse_scores(raw, ["a", "b", "c", "d", "e"])
    assert [s.window_id for s in valid] == ["a", "e"]
    assert valid[0].score == 80
    assert len(valid[1].reasons) == 400
    assert (returned, invalid) == (8, 6)


@pytest.mark.parametrize("raw", [None, [], {"scores": "nope"}, {"other": []}])
def test_parse_scores_bad_shape(raw):
    assert parse_scores(raw, ["a"]) == ([], 0, 0)


def test_score_windows_batches_and_records_costs(client, fake: FakeAnthropic, session):
    windows = make_windows(25)
    fake.add(echo_scores, echo_scores, echo_scores)
    outcome = score_windows(windows, campaign_in(rules_notes="Keep it PG."), client=client)
    assert outcome.calls == 3
    assert len(fake.requests) == 3
    assert len(outcome.scores) == 25
    assert outcome.validity == 1.0
    assert outcome.missing_ids == []
    body = fake.requests[0]
    assert body["model"] == "claude-haiku-5-5"  # config models.rank
    assert body["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert body["tools"][0]["input_schema"] == score_tool_schema()
    user = body["messages"][0]["content"]
    assert "Keep it PG." in user
    assert user.count("<window id=") == BATCH_SIZE
    assert "<examples>none yet</examples>" in user
    assert "temperature" not in body
    assert len(session.scalars(select(CostRow)).all()) == 6  # input + output per call


def test_fewshots_injected(client, fake):
    fake.add(echo_scores)
    shots = [Fewshot("positive", "He made the jump", "I made it!", "48k views", "2026-W47")]
    score_windows(make_windows(2), campaign_in(), client=client, fewshots=shots)
    user = fake.requests[0]["messages"][0]["content"]
    assert "hook: He made the jump" in user
    assert 'kind="positive"' in user


def test_missing_and_invalid_items_reported(client, fake):
    fake.add(tool_use(TOOL_NAME, {"scores": [item("9:0"), item("9:1", score=500), item("9:999")]}))
    outcome = score_windows(make_windows(3), campaign_in(), client=client)
    assert [s.window_id for s in outcome.scores] == ["9:0"]
    assert outcome.missing_ids == ["9:1", "9:2"]
    assert outcome.invalid_items == 2
    assert outcome.validity == pytest.approx(1 / 3)


def test_refused_batch_is_skipped_not_fatal(client, fake):
    fake.add(
        message([{"type": "text", "text": ""}], stop_reason="refusal"),
        echo_scores,
    )
    outcome = score_windows(make_windows(15), campaign_in(), client=client)
    assert len(outcome.failed_batches) == 1
    assert "refusal" in outcome.failed_batches[0]
    assert [s.window_id for s in outcome.scores] == [f"9:{i}" for i in range(10, 15)]
    assert len(outcome.missing_ids) == 10


def test_scores_real_fixture_windows(client, fake, talk):
    windows = build_windows(talk, keep=12)
    fake.add(echo_scores, echo_scores)
    outcome = score_windows(windows, campaign_in(content_kind="game"), client=client)
    assert {s.window_id for s in outcome.scores} == {w.window_id for w in windows}


def test_no_windows_no_calls(client, fake):
    outcome = score_windows([], campaign_in(), client=client)
    assert outcome.calls == 0
    assert fake.requests == []
    assert outcome.validity == 1.0
