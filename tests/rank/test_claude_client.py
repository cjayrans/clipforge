"""B3: Anthropic client: request shape, retries, cost recording, spend-cap guard, stop reasons."""

from __future__ import annotations

import anthropic
import pytest
from sqlalchemy import select

from clipforge import costs
from clipforge.db import CostRow
from clipforge.rank.claude_client import ClaudeClient, RankError, nullable, strict_object
from tests.rank.anthropic_mock import FakeAnthropic, error, json_text, message, tool_use

SCHEMA = strict_object({"x": {"type": "integer"}})


def call_tool(client: ClaudeClient):
    return client.forced_tool(
        model="claude-haiku-5-5",
        system="sys",
        user="hi",
        tool_name="t",
        tool_description="d",
        input_schema=SCHEMA,
    )


def call_json(client: ClaudeClient, **kw):
    return client.json_output(model="claude-sonnet-5-5", system="sys", user="hi", schema=SCHEMA, **kw)


def test_forced_tool_request_shape_and_result(client, fake: FakeAnthropic):
    fake.add(tool_use("t", {"x": 3}))
    result = call_tool(client)
    assert result.data == {"x": 3}
    assert (result.input_tokens, result.output_tokens, result.attempts) == (1000, 200, 1)
    body = fake.requests[0]
    assert fake.paths == ["/v1/messages"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tool_choice"] == {"type": "tool", "name": "t"}
    assert body["tools"][0]["strict"] is True
    assert body["tools"][0]["input_schema"]["additionalProperties"] is False
    for banned in ("temperature", "top_p", "top_k", "thinking"):
        assert banned not in body
    # anthropic 2.x sends the key as a bearer token and pins API version 2026-10-21 (no sampling params at all).
    assert fake.headers[0]["authorization"] == "Bearer sk-test-not-a-real-key"
    assert fake.headers[0]["anthropic-version"] == "2026-10-21"


def test_effort_goes_in_output_config(client, fake):
    fake.add(tool_use("t", {"x": 1}))
    client.forced_tool(
        model="claude-haiku-5-5", system="s", user="u", tool_name="t", tool_description="d",
        input_schema=SCHEMA, effort="low",
    )  # fmt: skip
    assert fake.requests[0]["output_config"] == {"effort": "low"}


def test_json_output_request_shape(client, fake):
    fake.add(json_text({"x": 5}))
    result = call_json(client, effort="low")
    assert result.data == {"x": 5}
    body = fake.requests[0]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"}
    assert "tool_choice" not in body  # Sonnet 5.5 rejects forced tool choice
    assert "tools" not in body
    assert "temperature" not in body


def test_costs_recorded_per_call(client, fake, session):
    fake.add(tool_use("t", {"x": 1}, input_tokens=2_000_000, output_tokens=1_000_000))
    call_tool(client)
    rows = session.scalars(select(CostRow).order_by(CostRow.id)).all()
    assert [(r.service, r.model, r.unit_kind, r.units) for r in rows] == [
        ("anthropic", "claude-haiku-5-5", "input_tokens", 2_000_000),
        ("anthropic", "claude-haiku-5-5", "output_tokens", 1_000_000),
    ]
    # example config: haiku $0.10 in / $0.50 out per MTok
    assert sum(r.usd for r in rows) == pytest.approx(0.2 + 0.5)


def test_cost_priced_by_requested_model(client, fake, session):
    fake.add(json_text({"x": 1}, model="claude-sonnet-5-5-some-snapshot", input_tokens=1_000_000, output_tokens=0))
    call_json(client)
    rows = session.scalars(select(CostRow)).all()
    assert {r.model for r in rows} == {"claude-sonnet-5-5"}
    assert sum(r.usd for r in rows) == pytest.approx(2.0)


@pytest.mark.parametrize("status", [429, 500, 529, 408])
def test_retries_then_succeeds(client, fake, session, status):
    fake.add(error(status), tool_use("t", {"x": 1}))
    result = call_tool(client)
    assert result.attempts == 2
    assert len(fake.requests) == 2
    assert len(session.scalars(select(CostRow)).all()) == 2  # only the successful call is billed


def test_gives_up_after_three_attempts(client, fake, session):
    fake.add(error(529, "overloaded_error"), error(529, "overloaded_error"), error(529, "overloaded_error"))
    with pytest.raises(anthropic.InternalServerError):
        call_tool(client)
    assert len(fake.requests) == 3
    assert session.scalars(select(CostRow)).all() == []


def test_client_errors_are_not_retried(client, fake):
    fake.add(error(400, "invalid_request_error"))
    with pytest.raises(anthropic.BadRequestError):
        call_tool(client)
    assert len(fake.requests) == 1


def test_spend_cap_blocks_before_any_request(client, fake, session):
    costs.pause(session, "test cap")
    with pytest.raises(costs.SpendCapReached):
        call_tool(client)
    assert fake.requests == []


def test_month_to_date_over_cap_blocks(client, fake, session, app_config):
    costs.record(session, app_config, "anthropic", "claude-sonnet-5-5", 10_000_000, "output_tokens")  # $100
    with pytest.raises(costs.SpendCapReached):
        call_json(client)
    assert fake.requests == []


def test_refusal_raises(client, fake, session):
    fake.add(
        message(
            [{"type": "text", "text": ""}],
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "cyber", "explanation": None},
        )
    )
    with pytest.raises(RankError, match="refusal"):
        call_tool(client)
    assert len(session.scalars(select(CostRow)).all()) == 2  # a refused call is still billed


def test_max_tokens_raises(client, fake):
    fake.add(json_text({"x": 1}, stop_reason="max_tokens"))
    with pytest.raises(RankError, match="max_tokens"):
        call_json(client)


def test_missing_tool_call_raises(client, fake):
    fake.add(message([{"type": "text", "text": "no tool"}], stop_reason="end_turn"))
    with pytest.raises(RankError, match="did not call"):
        call_tool(client)


def test_bad_json_raises(client, fake):
    fake.add(message([{"type": "text", "text": "{not json"}], model="claude-sonnet-5-5"))
    with pytest.raises(RankError, match="invalid JSON"):
        call_json(client)


def test_empty_text_raises(client, fake):
    fake.add(message([{"type": "thinking", "thinking": "", "signature": "s"}], model="claude-sonnet-5-5"))
    with pytest.raises(RankError, match="no text"):
        call_json(client)


@pytest.mark.parametrize("key", [None, ""])
def test_missing_key_is_a_clear_error(session, app_config, key):
    with pytest.raises(RankError, match="ANTHROPIC_API_KEY is not set"):
        ClaudeClient(key, session=session, app=app_config)


def test_from_settings_uses_secret(settings, session, monkeypatch):
    with pytest.raises(RankError, match="not set"):
        ClaudeClient.from_settings(settings, session)


def test_schema_helpers():
    assert nullable({"type": "number"}) == {"anyOf": [{"type": "number"}, {"type": "null"}]}
    obj = strict_object({"a": {"type": "string"}, "b": {"type": "string"}}, required=["a"])
    assert obj == {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
        "additionalProperties": False,
    }
