"""Invariants 2 and 9 with a campaign's required caption text (a fake campaign with a channel link and a tag):
'#ad' first, then the copy, then the required tags, in order, always last."""

from __future__ import annotations

import pytest

from clipforge.compliance import ComplianceError, normalize_caption
from clipforge.schemas import CampaignIn, Platform
from tests.support.factories import campaign_in

LINK = "twitch.tv/fakestreamer"


def fake(**kw: object) -> CampaignIn:
    return campaign_in(name="Fake Streamer Clips", required_caption_tags=[LINK, "#FakeTag"], **kw)


@pytest.mark.parametrize("platform", [Platform.TIKTOK, Platform.INSTAGRAM, Platform.YOUTUBE])
def test_ad_then_copy_then_required_tags(platform: Platform) -> None:
    out = normalize_caption("He really tried that jump #GTA6 #fail", platform, fake())
    assert out == f"#ad He really tried that jump #GTA6 #fail {LINK} #FakeTag"


def test_required_tag_written_mid_copy_moves_to_the_end() -> None:
    out = normalize_caption(f"Watch {LINK} lose it #fakeTAG #GTA6", Platform.TIKTOK, fake())
    assert out == f"#ad Watch lose it #GTA6 {LINK} #FakeTag"
    assert out.count(LINK) == 1
    assert out.lower().count("#faketag") == 1


def test_required_tags_kept_when_caption_is_cut() -> None:
    out = normalize_caption("word " * 600, Platform.TIKTOK, fake())
    assert out.startswith("#ad ")
    assert out.endswith(f"… {LINK} #FakeTag")
    assert len(out) <= 2200


def test_json_may_name_them_required_caption_tags_or_required_tags() -> None:
    a = campaign_in(required_caption_tags=[LINK])
    b = campaign_in(required_tags=[LINK])
    assert a.required_tags == b.required_tags == [LINK]
    with pytest.raises(ValueError, match="not both"):
        campaign_in(required_tags=[LINK], required_caption_tags=[LINK])


def test_url_required_tag_never_goes_on_x() -> None:
    with pytest.raises(ComplianceError) as err:
        normalize_caption("Big jump", Platform.X, fake(allowed_platforms=["tiktok", "x"]))
    assert err.value.invariant == 9


def test_hashtag_only_campaign_still_publishes_on_x() -> None:
    out = normalize_caption("Big jump", Platform.X, campaign_in(required_caption_tags=["#FakeTag"]))
    assert out == "#ad Big jump #FakeTag"


def test_longer_link_containing_the_tag_is_removed_whole() -> None:
    out = normalize_caption(f"Clip from https://{LINK} and {LINK}/videos lol", Platform.TIKTOK, fake())
    assert out == f"#ad Clip from and lol {LINK} #FakeTag"
