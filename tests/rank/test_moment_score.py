"""Moment ranker v2: rubric schema and parsing, gates (content replaces the loud-trigger gate), window rules,
selection, and rank_moments end to end (content pass + signals + rubric + keyframe check)."""

from __future__ import annotations

from typing import Any

import pytest

from clipforge import costs
from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.moment_score import (
    BATCH_SIZE,
    MomentItem,
    apply_window_rules,
    best_distinct,
    evaluate,
    moment_tool_schema,
    parse_moment_scores,
    rank_moments,
    render_moments,
    weighted_score,
)
from clipforge.rank.moments import Moment, generate_moments
from clipforge.rank.vision import Frame
from clipforge.schemas import Platform
from tests.rank.anthropic_mock import FakeAnthropic
from tests.rank.moment_fakes import (
    GOOD_SCORES,
    REACTION_SCRIPT,
    found_by_in,
    gta_profile,
    loudness,
    moment_item,
    moment_tools,
    motion,
    proposal,
    text_of,
    tool_of,
    transcript,
    words_at,
)
from tests.support.factories import campaign_in

STYLE = "Humor and meme tone, relatable captions."
LOUD = loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0})
MOTION = motion(spikes={30.0: 25.0})


@pytest.fixture(scope="module")
def profile():
    return gta_profile()


@pytest.fixture(scope="module")
def moments(profile):
    tr = transcript(REACTION_SCRIPT)
    res = generate_moments(tr, profile, loudness_db=LOUD, motion=MOTION)
    return tr, sorted(res.moments, key=lambda m: m.start_s)


def item(m: Moment, **kw: Any) -> MomentItem:
    raw = moment_item(m.moment_id, m.start_s, m.end_s, **kw)
    (parsed,), _, _ = parse_moment_scores({"scores": [raw]}, [m.moment_id], gta_profile())
    return parsed


def bare(start: float, end: float, payoff: float, mid: str = "7:m1") -> Moment:
    return Moment(mid, 7, start, end, start + 1, payoff, None, (), 0.0, (), (), "", "content")


# -- schema and parsing ----------------------------------------------------------------------------


def test_tool_schema_has_the_six_rubric_scores_and_is_strict(profile) -> None:
    schema = moment_tool_schema(profile)
    item_schema = schema["properties"]["scores"]["items"]
    assert item_schema["additionalProperties"] is False
    props = item_schema["properties"]
    for key in ("hook", "arc", "arousal", "sendability", "clarity", "density"):
        assert props[key]["type"] == "integer"
    for gone in ("reaction", "event", "setup", "absurdity"):
        assert gone not in props
    assert props["has_trigger"]["type"] == "boolean"
    assert props["has_payoff"]["type"] == "boolean"
    assert "other" in props["archetype"]["enum"]
    assert set(item_schema["required"]) == set(props)


def test_parse_validates_scores_and_normalizes(profile) -> None:
    raw = {
        "scores": [
            moment_item("a", 10, 30, archetype="nope", skip_flags=["cutscene", "made_up"], reason=" x " * 300),
            moment_item("b", 10, 30, hook=11),  # out of range
            moment_item("c", 10, 30),  # not requested
            {**moment_item("d", 10, 30), "density": None},  # missing score
        ]
    }
    items, returned, invalid = parse_moment_scores(raw, ["a", "b", "d"], profile)
    assert returned == 4
    assert invalid == 3
    (a,) = items
    assert a.archetype == "other"
    assert a.skip_flags == ["cutscene"]
    assert len(a.reason) <= 200
    assert a.scores == GOOD_SCORES


def test_weighted_score_uses_the_profile_weights(profile) -> None:
    assert weighted_score(dict.fromkeys(profile.rubric_ids, 10), profile) == 100
    scores = {"hook": 10, "arc": 0, "arousal": 0, "sendability": 0, "clarity": 0, "density": 0}
    assert weighted_score(scores, profile) == 20.0  # hook weight 0.2
    assert weighted_score(GOOD_SCORES, profile, penalty=0.5) == pytest.approx(
        0.5 * 10 * (0.2 * 8 + 0.2 * 7 + 0.2 * 8 + 0.2 * 7 + 0.1 * 7 + 0.1 * 6)
    )


# -- gates -----------------------------------------------------------------------------------------


def names(e) -> list[str]:
    return [g.name for g in e.gates]


def test_gates_run_in_order(moments, profile) -> None:
    tr, (m, _) = moments
    e = evaluate(m, item(m), profile, words=tr.words, duration_s=90)
    assert e.score is not None
    assert e.dropped_by is None
    assert names(e)[-5:] == ["brief", "skip_rules", "content", "window", "min_score"]


def test_content_gate_replaces_the_loud_trigger_gate(moments, profile) -> None:
    tr, (m, _) = moments
    e = evaluate(m, item(m, has_payoff=False), profile, words=tr.words, duration_s=90)
    assert e.score is None
    assert e.dropped_by == "content"
    assert "no clear payoff" in e.gates[-1].detail
    # A quiet content-only candidate (no signal at all) passes on content alone.
    quiet = bare(40.0, 60.0, 58.0)
    e = evaluate(quiet, item(quiet), profile, words=tr.words, duration_s=90)
    assert e.score is not None


def test_skip_rules_reject_or_penalize(moments, profile) -> None:
    tr, (m, _) = moments
    assert evaluate(m, item(m, skip_flags=["story_reveal"]), profile, words=tr.words, duration_s=90).score is None
    full = evaluate(m, item(m), profile, words=tr.words, duration_s=90).score
    part = evaluate(m, item(m, skip_flags=["cutscene_part"]), profile, words=tr.words, duration_s=90).score
    assert part == pytest.approx(full * 0.6, abs=0.01)


def test_sexual_content_is_flagged_never_rejected(moments, profile) -> None:
    tr, (m, _) = moments
    e = evaluate(m, item(m, review_flags=["sexual"]), profile, words=tr.words, duration_s=90)
    assert e.score is not None
    assert e.item.review_flags == ["sexual"]


def test_min_score_threshold_is_unchanged_at_40(moments, profile) -> None:
    assert profile.gates.min_score == 40
    tr, (m, _) = moments
    low = dict.fromkeys(profile.rubric_ids, 3)
    e = evaluate(m, item(m, **low), profile, words=tr.words, duration_s=90)
    assert e.score is None
    assert e.dropped_by == "min_score"


# -- window rules ----------------------------------------------------------------------------------


def test_window_extends_to_the_payoff_instead_of_truncating(profile) -> None:
    m = bare(100.0, 118.0, 117.0)
    it = item(m, start_s=100.0, end_s=118.0, payoff_s=126.0)  # the payoff runs past the window
    d = apply_window_rules(m, it, profile, [], 3600)
    assert d.end_s == pytest.approx(126.0 + profile.window.post_roll_s)
    assert not d.too_long
    assert any("extended to the payoff" in n for n in d.notes)


def test_window_ends_right_after_the_payoff(profile) -> None:
    m = bare(100.0, 130.0, 115.0)
    d = apply_window_rules(m, item(m, start_s=100.0, end_s=130.0, payoff_s=115.0), profile, [], 3600)
    assert d.end_s == pytest.approx(116.5)


def test_no_dead_air_at_the_start(profile) -> None:
    words = words_at([(105.0, "no no no what did I do"), (110.0, "I sold the car")])
    m = bare(100.0, 120.0, 112.0)
    d = apply_window_rules(m, item(m, start_s=100.0, end_s=120.0, payoff_s=112.0), profile, words, 3600)
    first_word = 105.0
    assert first_word - d.start_s <= profile.window.max_lead_silence_s + 1e-6
    assert any("dead air" in n for n in d.notes)


def test_a_moment_longer_than_the_cap_is_dropped_not_cut(profile) -> None:
    m = bare(100.0, 140.0, 139.0)
    it = item(m, start_s=100.0, end_s=160.0, payoff_s=158.0)
    d = apply_window_rules(m, it, profile, [], 3600)
    assert d.too_long
    words = words_at([(100.0, "okay so")])
    e = evaluate(m, it, profile, words=words, duration_s=3600)
    assert e.score is None
    assert e.dropped_by == "window"


def test_model_window_outside_the_context_falls_back(profile) -> None:
    m = bare(1000.0, 1020.0, 1018.0)
    d = apply_window_rules(m, item(m, start_s=10.0, end_s=30.0, payoff_s=28.0), profile, [], 3600)
    assert not d.used_model
    assert d.start_s >= 1000.0 - 0.6


# -- prompt rendering ------------------------------------------------------------------------------


def test_candidates_carry_30s_of_context_and_found_by(moments, profile) -> None:
    tr, (m, _) = moments
    text = render_moments([m], tr.words, profile.window.context_s)
    assert f'found_by="{m.found_by}"' in text
    assert "before (context):" in text
    assert "after (context):" in text
    assert "[1.0] okay so we are just driving" in text  # ~25 s before the blunder window
    assert "[56.0] this road is long and quiet" in text  # after it


# -- selection -------------------------------------------------------------------------------------


def test_overlapping_picks_keep_the_better(moments, profile) -> None:
    tr, (m, _) = moments
    twin = bare(m.start_s + 1, m.end_s + 1, m.payoff_s + 1, "7:m2")
    a = evaluate(m, item(m), profile, words=tr.words, duration_s=90)
    b = evaluate(twin, item(twin, hook=10, arc=10), profile, words=tr.words, duration_s=90)
    picks, dropped = best_distinct([a, b], 8)
    assert [p.moment.moment_id for p in picks] == ["7:m2"]
    assert dropped == 1


# -- end to end ------------------------------------------------------------------------------------


def frames_factory(windows):
    return lambda start, end, times: [Frame(t, b"\xff\xd8jpeg") for t in times]


def test_rank_moments_end_to_end(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    tr = transcript(REACTION_SCRIPT)

    def per(cid, s, e, beat):
        return {"archetype": "npc_absurd", "review_flags": ["sexual"]} if beat > 60 else {}

    fake.default = moment_tools(proposals=[proposal(44.0, 62.0, archetype="bit")], per_candidate=per)
    campaign = campaign_in(copy_style_notes=STYLE, required_tags=["#clip"], moment_profile="gta6-streamer")
    result = rank_moments(
        tr,
        campaign,
        [Platform.TIKTOK, Platform.INSTAGRAM],
        profile,
        client=client,
        loudness_db=LOUD,
        motion=MOTION,
        frames=frames_factory,
    )
    assert result.prompt_version == "propose_moments@2+score_moments@3+judge_frames@2+clip_copy@3"
    run = result.moment_run
    assert {e.moment.found_by for e in run.evaluated} >= {"content", "signals"}
    assert all(c.copy_ is None for c in result.candidates)  # copy comes later, for the night's picks only
    for c in result.candidates:
        assert c.moment.weights_version == "gta6-streamer-v2"
        assert set(c.moment.scores) == set(profile.rubric_ids)
        assert c.moment.vision is not None
        assert c.moment.vision["scene"] == "gameplay"
        assert 12 <= c.end_s - c.start_s <= 45
    flagged = [c for c in result.candidates if "sexual" in c.moment.review_flags]
    assert flagged  # flagged, still a candidate
    assert "moments (gta6-streamer-v2)" in result.detail()
    assert "vision: " in result.detail()

    tools = [tool_of(b) for b in fake.requests]
    assert tools[0] == "propose_moments"
    assert "score_moments" in tools
    assert "judge_frames" in tools
    score_body = next(b for b in fake.requests if tool_of(b) == "score_moments")
    assert score_body["model"] == "claude-haiku-5-5"
    assert score_body["tool_choice"] == {"type": "tool", "name": "score_moments"}
    assert "temperature" not in score_body
    assert "Humor and meme tone" in text_of(score_body)
    vision_body = next(b for b in fake.requests if tool_of(b) == "judge_frames")
    images = [blk for blk in vision_body["messages"][0]["content"] if blk["type"] == "image"]
    assert images
    assert images[0]["source"]["media_type"] == "image/jpeg"
    assert len(images) <= profile.vision.max_frames
    assert run.vision.checked == len(run.vision.per_candidate_usd) > 0


def test_an_other_moment_outside_the_catalogue_survives_selection(
    fake: FakeAnthropic, client: ClaudeClient, profile
) -> None:
    """The catalogue is examples, not limits: a moment no archetype describes is scored and picked."""
    tr = transcript([(float(t), "so the thing about taxes in this city is wild") for t in range(0, 88, 3)])
    fake.default = moment_tools(
        proposals=[proposal(20.0, 40.0, archetype="tax_rant", reason="A relatable rant about in-game taxes.")],
        per_candidate=lambda cid, s, e, b: {"archetype": "tax_rant", "reason": "Relatable tax rant."},
    )
    result = rank_moments(tr, campaign_in(), [Platform.TIKTOK], profile, client=client)
    (c,) = result.candidates
    assert c.moment.archetype == "other"
    assert c.moment.found_by == "content"
    assert "Relatable tax rant" in c.reasons


def test_rank_moments_never_proposes_a_window_overlapping_used_footage(
    fake: FakeAnthropic, client: ClaudeClient, profile
) -> None:
    tr = transcript(REACTION_SCRIPT)
    fake.default = moment_tools()
    campaign = campaign_in(moment_profile="gta6-streamer")
    kwargs: dict[str, Any] = {"client": client, "loudness_db": LOUD, "motion": MOTION}
    base = rank_moments(tr, campaign, [Platform.TIKTOK], profile, **kwargs)
    assert len(base.candidates) == 2
    first = base.candidates[0]
    result = rank_moments(tr, campaign, [Platform.TIKTOK], profile, exclude=[(first.start_s, first.end_s)], **kwargs)
    for c in result.candidates:
        assert not (c.start_s < first.end_s and first.start_s < c.end_s)
    assert len(result.candidates) < len(base.candidates)


def test_batches_are_by_candidate_not_by_time(fake: FakeAnthropic, client: ClaudeClient, profile) -> None:
    script = []
    for k in range(12):
        base = 40.0 * k
        script += [(base, "okay"), (base + 10, "look at this"), (base + 11.2, "no no no"), (base + 12.5, "oh my god")]
    tr = transcript(script, duration_s=500.0)
    fake.default = moment_tools()
    rank_moments(tr, campaign_in(), [Platform.TIKTOK], profile, client=client)
    batches = [found_by_in(b) for b in fake.requests if tool_of(b) == "score_moments"]
    assert [len(b) for b in batches] == [BATCH_SIZE, 12 - BATCH_SIZE]


def test_spend_cap_stops_the_moment_ranker_before_any_request(
    fake: FakeAnthropic, client: ClaudeClient, profile, session
) -> None:
    costs.pause(session, "test cap")
    session.commit()
    with pytest.raises(costs.SpendCapReached):
        rank_moments(transcript(REACTION_SCRIPT), campaign_in(), [Platform.TIKTOK], profile, client=client)
    assert fake.requests == []


def test_long_window_starts_at_the_first_beat(profile) -> None:
    m = bare(100.0, 140.0, 139.0)
    d = apply_window_rules(
        m, item(m, start_s=100.0, end_s=141.0, first_beat_s=125.0, payoff_s=139.0), profile, [], 3600
    )
    assert d.start_s == pytest.approx(124.7)
    assert d.end_s == pytest.approx(140.5)
    assert d.end_s - d.start_s <= profile.window.target_max_s
    assert any("first beat" in n for n in d.notes)


def test_a_moment_that_needs_its_setup_may_run_past_30s(profile) -> None:
    m = bare(100.0, 140.0, 139.0)
    d = apply_window_rules(
        m, item(m, start_s=100.0, end_s=141.0, first_beat_s=101.0, payoff_s=139.0), profile, [], 3600
    )
    assert d.end_s - d.start_s > profile.window.target_max_s
    assert not d.too_long
    assert any("over the 30s target" in n for n in d.notes)


def test_end_is_payoff_plus_post_roll_on_a_word_edge(profile) -> None:
    setup = words_at([(100.0, "so this button buys the car")])
    words = setup + words_at([(116.2, "okay okay")])  # "okay" 116.2-116.5 contains payoff + 1.5 = 116.3
    m = bare(100.0, 130.0, 114.8)
    d = apply_window_rules(m, item(m, start_s=100.0, end_s=130.0, payoff_s=114.8), profile, words, 3600)
    assert d.end_s == pytest.approx(116.5)  # within 0.4 s: to the word's end
    words = words_at([(116.0, "aaaaaaaaaaaaa")])
    long_word = setup + [w.model_copy(update={"end_s": 117.5}) for w in words]  # ends 1.2 s after: cut before it
    d = apply_window_rules(m, item(m, start_s=100.0, end_s=130.0, payoff_s=114.8), profile, long_word, 3600)
    assert d.end_s == pytest.approx(116.0)
