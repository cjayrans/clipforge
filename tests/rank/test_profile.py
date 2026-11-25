"""Moment profiles: the repo profile loads, validation catches bad files, and the code stays niche-generic."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from clipforge.rank.profile import ProfileError, load_profile, parse_profile, profiles_dir
from clipforge.schemas import CampaignIn
from tests.support.factories import campaign_in

ROOT = Path(__file__).resolve().parents[2]


def _gta_raw() -> dict:
    return yaml.safe_load((profiles_dir() / "gta6-streamer.yaml").read_text())


def test_gta_profile_matches_the_research_starting_values() -> None:
    p = load_profile("gta6-streamer")
    w = p.weights
    assert (w.reaction, w.event, w.setup, w.absurdity) == (0.35, 0.25, 0.25, 0.15)
    assert p.archetype_ids == ["blunder", "shock", "npc_weirdness", "glitch", "chaos", "chat_interplay", "other"]
    win = p.window
    assert (win.pre_roll_min_s, win.pre_roll_max_s) == (2, 5)
    assert (win.min_s, win.target_max_s, win.max_s) == (12, 35, 45)
    assert p.gates.reaction_within_s == 3
    assert {r.id: r.action for r in p.skip_rules} == {
        "cutscene": "reject",
        "story_reveal": "reject",
        "media_alone": "reject",
        "cutscene_part": "penalize",
    }
    assert {r.id for r in p.review_flags} == {"sexual", "graphic"}
    assert "no" in p.lexicon.exclamations  # YAML 1.1 would read a bare `no` as false


def test_every_campaign_profile_reference_exists() -> None:
    names = set()
    for path in sorted((ROOT / "campaigns").glob("*.json")):
        c = CampaignIn.model_validate(json.loads(path.read_text()))
        if c.moment_profile:
            load_profile(c.moment_profile)
            names.add(c.moment_profile)
    assert "gta6-streamer" in names  # Boeshi opted in


def test_boeshi_uses_the_gta_profile_and_keeps_its_style_notes() -> None:
    c = CampaignIn.model_validate(json.loads((ROOT / "campaigns" / "boeshi-gta6.json").read_text()))
    assert c.moment_profile == "gta6-streamer"
    assert c.copy_style_notes


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r["weights"].update(reaction=0.5), "sum to 1.0"),
        (lambda r: r.update(archetypes=[a for a in r["archetypes"] if a["id"] != "other"]), "include 'other'"),
        (lambda r: r["window"].update(max_s=120), "max_s"),
        (lambda r: r["window"].update(pre_roll_s=9), "pre_roll"),
        (lambda r: r["archetypes"].append(dict(r["archetypes"][0])), "unique"),
        (lambda r: r["lexicon"].update(laughter_patterns=["(unclosed"]), "laughter pattern"),
        (lambda r: r.update(surprise=1), "Extra inputs"),
    ],
)
def test_invalid_profiles_are_refused(change, message) -> None:
    raw = _gta_raw()
    change(raw)
    with pytest.raises(ProfileError, match=message):
        parse_profile(yaml.safe_dump(raw))


@pytest.mark.parametrize("name", ["../secrets", "a/b", "", "UPPER"])
def test_profile_names_cannot_leave_the_directory(name: str) -> None:
    with pytest.raises(ProfileError):
        load_profile(name)


def test_missing_profile(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="not found"):
        load_profile("nope", tmp_path)


def test_file_name_must_match(tmp_path: Path) -> None:
    (tmp_path / "other.yaml").write_text((profiles_dir() / "gta6-streamer.yaml").read_text())
    with pytest.raises(ProfileError, match="does not match"):
        load_profile("other", tmp_path)


def test_campaign_profile_name_is_validated() -> None:
    with pytest.raises(ValidationError, match="moment_profile"):
        campaign_in(moment_profile="../x")


def test_no_niche_terms_in_generic_code_or_ranker_prompt() -> None:
    """Archetypes, words and policy live in the profile; the code and prompt name no game, streamer or publisher."""
    files = [*(ROOT / "src/clipforge/rank").glob("*.py"), ROOT / "src/clipforge/track/moments.py"]
    files.append(ROOT / "prompts/score_moments.md")
    pattern = re.compile(r"gta|grand theft|rockstar|boeshi|wanted level|weazel", re.I)
    for f in files:
        assert not pattern.search(f.read_text()), f
