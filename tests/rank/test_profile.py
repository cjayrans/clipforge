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


def test_gta_profile_v2_catalogue_rubric_and_weights() -> None:
    p = load_profile("gta6-streamer")
    assert p.weights_version == "gta6-streamer-v2"
    assert p.rubric_ids == ["hook", "arc", "arousal", "sendability", "clarity", "density"]
    assert p.weights == {"hook": 0.2, "arc": 0.2, "arousal": 0.2, "sendability": 0.2, "clarity": 0.1, "density": 0.1}
    assert p.archetype_ids == [
        "blunder", "npc_absurd", "glitch", "chaos", "discovery", "bit", "chat_interplay", "other",
    ]  # fmt: skip
    discovery = next(a for a in p.archetypes if a.id == "discovery")
    assert "secret" in discovery.copy_hint
    assert discovery.examples
    assert discovery.signs
    assert "loud reactions to nothing" in p.reject_examples
    win = p.window
    assert (win.min_s, win.target_min_s, win.target_max_s, win.max_s) == (12, 15, 30, 45)
    assert win.context_s == 30
    assert (p.content_pass.segment_s, p.content_pass.overlap_s) == (720, 120)
    assert p.vision.enabled
    assert p.vision.max_frames == 8
    assert p.vision.frame_every_s == 2.5
    assert p.gates.min_score == 40  # unchanged threshold (now on the six-part rubric)
    assert {r.id: r.action for r in p.skip_rules} == {
        "cutscene": "reject",
        "story_reveal": "reject",
        "media_alone": "reject",
        "no_event": "reject",
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
    assert "gta6-streamer" in names  # Boeshi, Dylan Page and AsianJeff opted in


def test_boeshi_uses_the_gta_profile_and_keeps_its_style_notes() -> None:
    c = CampaignIn.model_validate(json.loads((ROOT / "campaigns" / "boeshi-gta6.json").read_text()))
    assert c.moment_profile == "gta6-streamer"
    assert c.copy_style_notes


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r["weights"].update(hook=0.5), "sum to 1.0"),
        (lambda r: r["weights"].pop("density"), "exactly the rubric ids"),
        (lambda r: r["content_pass"].update(overlap_s=600), "at most half"),
        (lambda r: r.update(archetypes=[a for a in r["archetypes"] if a["id"] != "other"]), "include 'other'"),
        (lambda r: r["window"].update(max_s=120), "max_s"),
        (lambda r: r["window"].update(target_min_s=40), "target_min_s"),
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
    files += [ROOT / f"prompts/{name}.md" for name in ("score_moments", "propose_moments", "judge_frames")]
    pattern = re.compile(r"gta|grand theft|rockstar|boeshi|wanted level|weazel", re.I)
    for f in files:
        assert not pattern.search(f.read_text()), f
