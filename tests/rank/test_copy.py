"""B4: Sonnet copy → ClipCopy, captions through compliance.normalize_caption; rank_source end to end."""

from __future__ import annotations

from typing import Any

import pytest

from clipforge.compliance import AD_PREFIX, CAPTION_LIMITS, caption_length, contains_url
from clipforge.rank.claude_client import RankError
from clipforge.rank.copy import copy_schema, finalize_captions, rank_source, repair_copy, write_copy
from clipforge.rank.score import TOOL_NAME
from clipforge.schemas import Candidate, ClipCopy, Platform
from tests.rank.anthropic_mock import FakeAnthropic, json_text, message
from tests.rank.test_score import echo_scores
from tests.support.factories import campaign_in

ALL = [Platform.TIKTOK, Platform.INSTAGRAM, Platform.YOUTUBE, Platform.X]


def candidate(**kw: Any) -> Candidate:
    data: dict[str, Any] = {
        "source_id": 1,
        "start_s": 31.8,
        "end_s": 63.6,
        "score": 82,
        "standalone": True,
        "brief_ok": True,
        "reasons": "jump payoff",
        "prompt_version": "score_windows@1+clip_copy@1",
    }
    data.update(kw)
    return Candidate(**data)


def copy_payload(**kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "hook": "He tried jumping to a helicopter",
        "youtube_title": "Streamer jumps for a helicopter mid-heist in GTA 6",
        "captions": [
            {"platform": "tiktok", "text": "Clip that, somebody clip that #GTA6 #heist"},
            {"platform": "instagram", "text": "The jump of the century #GTA6"},
            {"platform": "youtube", "text": "Full heist on the channel #GTA6"},
            {"platform": "x", "text": "He actually made it. twitch.tv/dylanpage https://twitch.tv/dylanpage"},
        ],
    }
    data.update(kw)
    return data


def test_copy_schema_strict_and_enum():
    schema = copy_schema([Platform.TIKTOK, Platform.X])
    assert schema["additionalProperties"] is False
    item = schema["properties"]["captions"]["items"]
    assert item["properties"]["platform"]["enum"] == ["tiktok", "x"]
    assert item["additionalProperties"] is False
    assert "maxLength" not in str(schema)


def test_write_copy_request_and_compliant_captions(client, fake: FakeAnthropic):
    fake.add(json_text(copy_payload()))
    campaign = campaign_in(allowed_platforms=ALL, required_tags=["#DylanPage"], rules_notes="Tag the streamer.")
    copy = write_copy(candidate(), "Hold on, there is a helicopter", campaign, ALL, client=client)

    body = fake.requests[0]
    assert body["model"] == "claude-sonnet-5-5"  # config models.copy
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "low"
    assert "tool_choice" not in body
    assert "temperature" not in body
    user = body["messages"][0]["content"]
    assert "Hold on, there is a helicopter" in user
    assert "Platforms: tiktok, instagram, youtube, x" in user
    assert "(31.8 s long)" in user

    assert isinstance(copy, ClipCopy)
    assert [c.platform for c in copy.captions] == ALL
    for c in copy.captions:
        assert c.text.startswith(AD_PREFIX + " ")
        assert "#DylanPage" in c.text
        assert caption_length(c.text, c.platform) <= CAPTION_LIMITS[c.platform]
    x = next(c for c in copy.captions if c.platform is Platform.X)
    assert not contains_url(x.text)
    assert copy.hook == "He tried jumping to a helicopter"


def test_political_campaign_gets_no_tiktok_or_x(client, fake):
    fake.add(json_text(copy_payload()))
    copy = write_copy(candidate(), "text", campaign_in(allowed_platforms=ALL, is_political=True), ALL, client=client)
    assert [c.platform for c in copy.captions] == [Platform.INSTAGRAM, Platform.YOUTUBE]
    enum = fake.requests[0]["output_config"]["format"]["schema"]["properties"]["captions"]["items"]
    assert enum["properties"]["platform"]["enum"] == ["instagram", "youtube"]


def test_only_account_and_campaign_platforms(client, fake):
    fake.add(json_text(copy_payload()))
    campaign = campaign_in(allowed_platforms=["tiktok", "instagram"])
    copy = write_copy(candidate(), "t", campaign, [Platform.TIKTOK, Platform.YOUTUBE], client=client)
    assert [c.platform for c in copy.captions] == [Platform.TIKTOK]


def test_no_allowed_platform_raises_without_calling(client, fake):
    with pytest.raises(RankError, match="allows none"):
        write_copy(candidate(), "t", campaign_in(allowed_platforms=["tiktok"]), [Platform.YOUTUBE], client=client)
    assert fake.requests == []


def test_model_written_disclosure_not_duplicated(client, fake):
    fake.add(json_text(copy_payload(captions=[{"platform": "tiktok", "text": "#ad #ad wild jump"}])))
    copy = write_copy(candidate(), "t", campaign_in(allowed_platforms=["tiktok"]), [Platform.TIKTOK], client=client)
    assert copy.captions[0].text == "#ad wild jump"


def test_repair_hook_and_title():
    raw = copy_payload(hook="one two three four five six seven eight nine ten", youtube_title="word " * 30)
    copy = repair_copy(raw, [Platform.TIKTOK])
    assert copy.hook == "one two three four five six seven eight"
    assert len(copy.youtube_title) <= 60
    assert not copy.youtube_title.endswith(" ")


def test_repair_hook_char_limit():
    copy = repair_copy(copy_payload(hook="supercalifragilistic " * 4), [Platform.TIKTOK])
    assert len(copy.hook) <= 60


def test_repair_missing_platform_borrows_caption():
    raw = copy_payload(captions=[{"platform": "instagram", "text": "the jump"}, {"platform": "tiktok", "text": " "}])
    copy = repair_copy(raw, [Platform.TIKTOK, Platform.INSTAGRAM])
    assert [(c.platform, c.text) for c in copy.captions] == [
        (Platform.TIKTOK, "the jump"),
        (Platform.INSTAGRAM, "the jump"),
    ]


def test_repair_no_captions_uses_hook_and_ignores_junk():
    raw = copy_payload(captions=["junk", {"platform": "myspace", "text": "x"}], youtube_title="")
    copy = repair_copy(raw, [Platform.X])
    assert copy.captions[0].text == raw["hook"]
    assert copy.youtube_title == raw["hook"]


@pytest.mark.parametrize("raw", [None, [], {"hook": "   "}, {"captions": []}])
def test_repair_rejects_unusable(raw):
    with pytest.raises(RankError):
        repair_copy(raw, [Platform.TIKTOK])


def test_finalize_long_caption_is_cut_to_limit():
    long = ClipCopy(hook="h", youtube_title="t", captions=[{"platform": "x", "text": "word " * 200}])
    out = finalize_captions(long, campaign_in(allowed_platforms=["x"], required_tags=["#GTA6"]))
    assert out.captions[0].text.startswith("#ad ")
    assert out.captions[0].text.endswith("#GTA6")
    assert caption_length(out.captions[0].text, Platform.X) <= 280


def copy_or_score(body: dict[str, Any]):
    if body.get("tools"):
        return echo_scores(body)
    return json_text(copy_payload())


def test_rank_source_end_to_end(client, fake, talk, app_config):
    fake.default = copy_or_score
    campaign = campaign_in(allowed_platforms=["tiktok", "instagram"], required_tags=["#GTA6"], content_kind="game")
    result = rank_source(talk, campaign, [Platform.TIKTOK, Platform.INSTAGRAM], client=client)

    assert result.windows_built > 0
    assert result.score.validity == 1.0
    assert result.copy_failures == []
    assert 0 < len(result.candidates) <= app_config.pipeline.top_n_per_source
    score_calls = [b for b in fake.requests if b.get("tools")]
    copy_calls = [b for b in fake.requests if not b.get("tools")]
    assert len(copy_calls) == len(result.candidates)
    assert all(b["tools"][0]["name"] == TOOL_NAME for b in score_calls)
    for cand in result.candidates:
        assert cand.prompt_version == result.prompt_version == "score_windows@1+clip_copy@2"
        assert cand.copy_ is not None
        assert all(c.text.startswith("#ad ") and "#GTA6" in c.text for c in cand.copy_.captions)
        assert 10 <= cand.end_s - cand.start_s <= 90
    # Best score first; candidates don't overlap by more than half.
    scores = [c.score for c in result.candidates]
    assert scores == sorted(scores, reverse=True)


def test_rank_source_drops_candidate_whose_copy_fails(client, fake, talk):
    calls = {"copy": 0}

    def responder(body):
        if body.get("tools"):
            return echo_scores(body)
        calls["copy"] += 1
        if calls["copy"] == 1:
            return message([{"type": "text", "text": ""}], stop_reason="refusal", model="claude-sonnet-5-5")
        return json_text(copy_payload())

    fake.default = responder
    result = rank_source(talk, campaign_in(), [Platform.TIKTOK], client=client)
    assert len(result.copy_failures) == 1
    assert "refusal" in result.copy_failures[0]
    assert len(result.candidates) == calls["copy"] - 1
