"""Content-first moment finding: segments, the prompt (catalogue as examples), parsing, edge dedupe, merge."""

from __future__ import annotations

from typing import Any

import pytest

from clipforge.rank.candidates import merge_candidates
from clipforge.rank.catalogue import CATALOGUE_NOTE, render_catalogue
from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.content_pass import (
    TOOL_NAME,
    ContentMoment,
    Segment,
    dedupe_proposals,
    parse_proposals,
    plan_segments,
    proposal_schema,
    run_content_pass,
)
from clipforge.rank.moments import generate_moments, sorted_words
from tests.rank.anthropic_mock import FakeAnthropic, tool_use
from tests.rank.moment_fakes import REACTION_SCRIPT, gta_profile, loudness, proposal, propose, text_of, transcript
from tests.support.factories import campaign_in


@pytest.fixture(scope="module")
def profile():
    return gta_profile()


def talk(duration: float, every: float = 2.0) -> list[tuple[float, str]]:
    return [(t * every, "and we keep on going down this road") for t in range(int(duration / every))]


def test_segments_overlap_and_cover_the_whole_vod() -> None:
    segs = plan_segments(8 * 3600.0, 720, 120)
    assert segs[0].start_s == 0
    assert segs[-1].end_s == 8 * 3600.0
    for a, b in zip(segs, segs[1:], strict=False):
        assert a.end_s - b.start_s == pytest.approx(120)  # 2 min overlap
    assert plan_segments(300, 720, 120) == [Segment(0, 0.0, 300.0)]
    assert plan_segments(0, 720, 120) == []


def test_any_45s_moment_lies_whole_inside_some_segment() -> None:
    segs = plan_segments(5000.0, 720, 120)
    for start in range(0, 4955, 7):
        assert any(s.start_s <= start and start + 45 <= s.end_s for s in segs), start


def test_catalogue_is_rendered_as_examples_not_limits(profile) -> None:
    text = render_catalogue(profile)
    assert text.startswith(CATALOGUE_NOTE)
    assert "not a list of allowed moments" in CATALOGUE_NOTE
    assert "misclicked purchase" in text
    assert "selfie stick" in text
    assert "throwing the egg off the tower" in text
    assert '"no no no", "what did I just do"' in text  # signs come through
    schema = proposal_schema(profile)
    enum = schema["properties"]["moments"]["items"]["properties"]["archetype"]["enum"]
    assert "other" in enum  # any moment outside the catalogue can be proposed


def test_content_pass_prompt_and_request_shape(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    fake.default = propose([proposal(24.0, 41.0)])
    out = run_content_pass(transcript(REACTION_SCRIPT), campaign_in(rules_notes="No spoilers."), profile, client=client)
    (body,) = fake.requests
    assert body["model"] == "claude-haiku-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert body["tools"][0]["strict"] is True
    assert "temperature" not in body
    system = body["system"]
    assert "not a list of allowed moments" in system
    assert "loud reactions to nothing" in system  # reject list
    assert "`sendability`" in system
    assert "`density`" in system
    assert "quiet and deadpan moments count" in system
    user = text_of(body)
    assert "[31.0] no no no" in user  # global timestamps
    assert "No spoilers." in user
    assert len(out.moments) == 1
    assert out.calls == 1


def test_a_moment_outside_the_catalogue_is_kept_as_other(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    fake.default = propose([proposal(24.0, 41.0, archetype="interpretive_dance", reason="He narrates his own life.")])
    out = run_content_pass(transcript(REACTION_SCRIPT), campaign_in(), profile, client=client)
    (m,) = out.moments
    assert m.archetype == "other"
    assert m.reason == "He narrates his own life."


def test_parse_clamps_to_the_segment_and_marks_edges(profile) -> None:
    seg = Segment(1, 600.0, 1320.0)
    raw = {
        "moments": [
            proposal(590.0, 615.0),  # starts before the segment: clamped, at the inner edge
            proposal(900.0, 925.0, confidence=1.7),  # confidence clamped
            proposal(1000.0, 1001.0),  # too short
            {"start_s": 1},  # invalid
        ]
    }
    got, returned, invalid = parse_proposals(raw, seg, profile, 3600.0)
    assert returned == 4
    assert invalid == 2
    by_start = {m.start_s: m for m in got}
    assert by_start[600.0].edge is True
    assert by_start[900.0].edge is False
    assert by_start[900.0].confidence == 1.0


def test_dedupe_prefers_the_whole_proposal_over_the_edge_one() -> None:
    cut = ContentMoment(700.0, 720.0, 701.0, 719.0, "blunder", 0.9, "cut at the segment end", (0,), edge=True)
    whole = ContentMoment(700.0, 731.0, 701.0, 729.0, "blunder", 0.7, "whole", (1,), edge=False)
    (m,) = dedupe_proposals([cut, whole], 45)
    assert (m.start_s, m.end_s, m.payoff_s) == (700.0, 731.0, 729.0)
    assert m.segments == (0, 1)
    assert m.confidence == 0.9
    assert m.edge is False


def test_quiet_segments_are_not_sent(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    fake.default = propose([])
    tr = transcript([(10.0, "hi"), (1500.0, "and we keep going down this road now " * 6)], duration_s=1600)
    out = run_content_pass(tr, campaign_in(), profile, client=client)
    assert out.segments == 3
    assert out.skipped_quiet == 2
    assert out.calls == 1


def test_a_failed_segment_is_skipped_and_reported(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    def respond(body: dict[str, Any]):
        return tool_use("wrong_tool", {})

    fake.default = respond
    out = run_content_pass(transcript(talk(1500)), campaign_in(), profile, client=client)
    assert out.calls == out.segments
    assert len(out.failed_segments) == out.segments
    assert out.moments == []


def test_merge_marks_found_by(profile) -> None:
    tr = transcript(REACTION_SCRIPT)
    sig = generate_moments(tr, profile, loudness_db=loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0}))
    blunder = next(m for m in sig.moments if m.start_s < 35)
    content = [
        ContentMoment(blunder.start_s, blunder.end_s, 30.0, 39.0, "blunder", 0.9, "sells his car", (0,)),
        ContentMoment(45.0, 60.0, 47.0, 58.0, "bit", 0.6, "deadpan road trip bit", (0,)),
    ]
    merged = merge_candidates(
        content,
        sig.moments,
        source_id=7,
        words=sorted_words(tr),
        events=sig.events,
        profile=profile,
        duration_s=90.0,
    )
    found = {m.archetype_hint or "signal": m.found_by for m in merged}
    assert found["blunder"] == "both"
    assert found["bit"] == "content"
    assert "signal" in found
    assert found["signal"] == "signals"
    both = next(m for m in merged if m.found_by == "both")
    assert both.prescore > 0
    assert both.confidence == 0.9
    assert both.reactions
    assert merged[0].found_by == "both"  # agreement ranks first
