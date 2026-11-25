"""Haiku moment ranker: tool schema, parsing, post-score gates, window rules, selection, rank_moments end to end."""

from __future__ import annotations

from typing import Any

import pytest

from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.moment_score import (
    MomentItem,
    apply_window_rules,
    gate_and_score,
    moment_tool_schema,
    parse_moment_scores,
    prompt_values,
    rank_moments,
    select_moments,
    weighted_score,
)
from clipforge.rank.moments import generate_moments
from clipforge.rank.prompts import load_prompt
from clipforge.schemas import Platform
from tests.rank.anthropic_mock import FakeAnthropic, json_text
from tests.rank.moment_fakes import (
    REACTION_SCRIPT,
    echo_moments,
    gta_profile,
    loudness,
    moment_item,
    motion,
    transcript,
)
from tests.support.factories import campaign_in

STYLE = "Humor and meme tone, relatable captions."


@pytest.fixture(scope="module")
def profile():
    return gta_profile()


@pytest.fixture(scope="module")
def moments(profile):
    tr = transcript(REACTION_SCRIPT)
    ms, _ = generate_moments(
        tr, profile, loudness_db=loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0}), motion=motion(spikes={30.0: 25.0})
    )
    return tr, ms


def item(m, **kw: Any) -> MomentItem:
    return MomentItem.model_validate(moment_item(m.moment_id, m.start_s, m.end_s, **kw))


# -- schema and parsing ----------------------------------------------------------------------------


def test_tool_schema_is_strict_compatible_and_uses_profile_enums(profile) -> None:
    schema = moment_tool_schema(profile)

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
    props = schema["properties"]["scores"]["items"]["properties"]
    assert props["archetype"]["enum"] == profile.archetype_ids
    assert props["skip_flags"]["items"]["enum"] == ["cutscene", "story_reveal", "media_alone", "cutscene_part"]
    assert props["review_flags"]["items"]["enum"] == ["sexual", "graphic"]


def test_prompt_system_renders_from_the_profile(profile) -> None:
    prompt = load_prompt("score_moments")
    values = prompt_values(profile, campaign_in(copy_style_notes=STYLE), [])
    from clipforge.rank.moment_score import _fill

    system = _fill(prompt.system, values)
    assert "$" not in system
    assert "`blunder` (Streamer blunder with a big reaction)" in system
    assert "`cutscene` (Mostly cutscene)" in system
    assert "Start 2-5 seconds" in system
    assert "never more than 45" in system
    assert "Aim for 12-35 seconds" in system
    assert "Do NOT set it\n  false for sexual or graphic content" in system


def test_parse_drops_bad_items_and_normalises(profile) -> None:
    raw = {
        "scores": [
            moment_item("a", 0, 20, archetype="made_up", skip_flags=["cutscene", "nope"], reason="x " * 300),
            moment_item("a", 0, 20),  # duplicate
            moment_item("b", 0, 20, reaction=11),  # out of range
            moment_item("zzz", 0, 20),  # not requested
            {"candidate_id": "c"},  # incomplete
        ]
    }
    valid, returned, invalid = parse_moment_scores(raw, ["a", "b", "c"], profile)
    assert (returned, invalid) == (5, 4)
    assert valid[0].archetype == "other"
    assert valid[0].skip_flags == ["cutscene"]
    assert len(valid[0].reason) <= 200
    assert parse_moment_scores("junk", ["a"], profile) == ([], 0, 0)


# -- gates and scoring -----------------------------------------------------------------------------


def test_weighted_score_uses_the_initial_weights(profile, moments) -> None:
    m = moments[1][0]
    it = item(m, reaction=10, event=0, setup=0, absurdity=0)
    assert weighted_score(it, profile) == 35.0
    it = item(m, reaction=8, event=6, setup=7, absurdity=9)
    assert weighted_score(it, profile) == pytest.approx(10 * (0.35 * 8 + 0.25 * 6 + 0.25 * 7 + 0.15 * 9))


def test_skip_rules_reject_or_penalize(profile, moments) -> None:
    m = moments[1][0]
    score, gates, _ = gate_and_score(m, item(m, skip_flags=["cutscene"]), profile)
    assert score is None
    assert gates[-1].name == "skip_rules"
    assert gates[-1].passed is False
    score, gates, penalty = gate_and_score(m, item(m, skip_flags=["cutscene_part"]), profile)
    assert penalty == 0.6
    assert score == pytest.approx(weighted_score(item(m), profile) * 0.6, abs=0.01)
    assert "x0.6" in next(g for g in gates if g.name == "skip_rules").detail
    score, gates, _ = gate_and_score(m, item(m, brief_ok=False), profile)
    assert score is None
    assert gates[-1].name == "brief"


def test_sexual_content_is_flagged_not_rejected(profile, moments) -> None:
    tr, ms = moments
    m = ms[0]
    picks, _ = select_moments(
        [m],
        [item(m, review_flags=["sexual"])],
        profile,
        words=tr.words,
        duration_s=90,
        top_n=5,
        prompt_version="v",
    )
    assert len(picks) == 1
    assert picks[0].candidate.moment.review_flags == ["sexual"]
    assert picks[0].candidate.brief_ok is True


def test_sensitive_words_in_the_transcript_add_a_flag(profile) -> None:
    tr = transcript([(0, "okay"), (10, "look at this"), (11.2, "no no no"), (12.5, "the lap dance cost me everything")])
    ms, _ = generate_moments(tr, profile)
    picks, _ = select_moments(
        ms, [item(m) for m in ms], profile, words=tr.words, duration_s=90, top_n=5, prompt_version="v"
    )
    assert picks
    assert picks[0].candidate.moment.review_flags == ["sensitive words: lap dance"]


def test_min_score_gate(profile, moments) -> None:
    m = moments[1][0]
    score, gates, _ = gate_and_score(m, item(m, reaction=2, event=2, setup=2, absurdity=2), profile)
    assert score is None
    assert gates[-1].name == "min_score"
    assert gates[-1].passed is False


# -- window rules ----------------------------------------------------------------------------------


def test_model_window_used_only_when_it_obeys_the_rules(profile, moments) -> None:
    tr, ms = moments
    m = ms[0]  # trigger at 26.4 s, first reaction 31.0-31.3 s
    trig = m.trigger.t
    ok = item(m, start_s=trig - 3, end_s=trig + 14)
    assert apply_window_rules(m, ok, profile, tr.words, 90)[2] is True
    for bad in (
        item(m, start_s=trig - 8, end_s=trig + 14),  # starts more than 5 s before the trigger
        item(m, start_s=trig - 1, end_s=trig + 14),  # less than 2 s of setup
        item(m, start_s=trig - 3, end_s=trig + 50),  # over the 45 s hard cap
        item(m, start_s=trig - 3, end_s=trig + 4),  # under 12 s and cuts the reaction
    ):
        start, end, used = apply_window_rules(m, bad, profile, tr.words, 90)
        assert not used
        assert (start, end) == (m.start_s, m.end_s)


def test_selection_ranks_by_weighted_score_and_drops_overlaps(profile, moments) -> None:
    tr, ms = moments
    items = [item(ms[0], reaction=6), item(ms[1], reaction=9, archetype="npc_weirdness")]
    picks, stats = select_moments(ms, items, profile, words=tr.words, duration_s=90, top_n=5, prompt_version="v")
    assert [p.candidate.moment.archetype for p in picks] == ["npc_weirdness", "blunder"]
    first = picks[0].candidate
    assert first.score == round(first.moment.weighted)
    assert first.reasons.startswith("[npc_weirdness] ")
    assert {g.name for g in first.moment.gates} == {
        "trigger_reaction",
        "dead_footage",
        "brief",
        "skip_rules",
        "min_score",
    }
    assert first.moment.weights_version == "gta6-streamer-v1"
    # A second candidate for the same moment is dropped as an overlap.
    clone = ms[0].__class__(**{**ms[0].__dict__, "moment_id": "7:mclone"})
    picks, stats = select_moments(
        [ms[0], clone], [item(ms[0]), item(clone)], profile, words=tr.words, duration_s=90, top_n=5, prompt_version="v"
    )
    assert len(picks) == 1
    assert stats.overlaps_dropped == 1


# -- end to end ------------------------------------------------------------------------------------


def test_rank_moments_end_to_end(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    tr = transcript(REACTION_SCRIPT)
    copy_bodies: list[dict[str, Any]] = []

    def respond(body: dict[str, Any]):
        if body.get("tools"):
            return echo_moments(
                lambda cid, s, e, t: {"archetype": "npc_weirdness", "review_flags": ["sexual"]} if t > 60 else {}
            )(body)
        copy_bodies.append(body)
        return json_text(
            {
                "hook": "He sold his car for a dollar",
                "youtube_title": "Sold the car by mistake",
                "captions": [
                    {"platform": "tiktok", "text": "worst deal ever #gaming"},
                    {"platform": "instagram", "text": "worst deal ever #gaming"},
                ],
            }
        )

    fake.default = respond
    campaign = campaign_in(copy_style_notes=STYLE, required_tags=["#clip"], moment_profile="gta6-streamer")
    result = rank_moments(
        tr,
        campaign,
        [Platform.TIKTOK, Platform.INSTAGRAM],
        profile,
        client=client,
        loudness_db=loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0}),
        motion=motion(spikes={30.0: 25.0}),
    )
    assert result.prompt_version == "score_moments@1+clip_copy@3"
    assert [c.moment.archetype for c in result.candidates] == ["blunder", "npc_weirdness"]
    assert result.candidates[1].moment.review_flags == ["sexual"]
    for c in result.candidates:
        assert c.copy_ is not None
        assert all(cap.text.startswith("#ad ") and cap.text.endswith("#clip") for cap in c.copy_.captions)
        assert 12 <= c.end_s - c.start_s <= 45
    assert "moments (gta6-streamer-v1)" in result.detail()

    haiku = fake.requests[0]
    assert haiku["model"] == "claude-haiku-5-5"
    assert haiku["tool_choice"] == {"type": "tool", "name": "score_moments"}
    assert haiku["tools"][0]["strict"] is True
    assert "temperature" not in haiku
    assert "<candidate id=" in haiku["messages"][0]["content"]
    assert "Humor and meme tone" in haiku["messages"][0]["content"]  # copy_style_notes reach the ranker brief
    # The copywriter gets the ranker's note on what happens, and the campaign style.
    sonnet = copy_bodies[0]
    assert sonnet["model"] == "claude-sonnet-5-5"
    assert "tool_choice" not in sonnet
    assert "temperature" not in sonnet
    user = sonnet["messages"][0]["content"]
    assert "<moment>\nblunder: Sells his car for a dollar by mistake and screams.\n</moment>" in user.replace(
        "Streamer blunder with a big reaction", "blunder"
    )
    assert "Humor and meme tone" in user


def test_rank_moments_with_nothing_triggered_calls_no_api(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    tr = transcript([(float(t), "we keep talking about the plan") for t in range(0, 80, 2)])
    result = rank_moments(tr, campaign_in(), [Platform.TIKTOK], profile, client=client)
    assert result.candidates == []
    assert fake.requests == []
    assert "0 sent to the ranker" in result.detail()
