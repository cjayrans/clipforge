"""Signal candidates (moments-v2): pairing proposes, strong reactions hint, loudness is evidence only (pure)."""

from __future__ import annotations

import pytest

from clipforge.rank.moments import fit_window, generate_moments, pairs_with, timestamped_lines
from clipforge.rank.signals import Event
from tests.rank.moment_fakes import REACTION_SCRIPT, gta_profile, loudness, motion, transcript, words_at


@pytest.fixture(scope="module")
def profile():
    return gta_profile()


def gate(m, name):
    return next(g for g in m.gates if g.name == name)


def test_reaction_script_gives_the_two_moments(profile) -> None:
    tr = transcript(REACTION_SCRIPT)
    res = generate_moments(
        tr, profile, loudness_db=loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0}), motion=motion(spikes={30.0: 25.0})
    )
    ms = sorted(res.moments, key=lambda m: m.start_s)
    assert len(ms) == 2
    blunder, laugh = ms
    assert {r.kind for r in blunder.reactions} >= {"exclaim", "loud"}
    assert "laugh" in {r.kind for r in laugh.reactions}
    assert res.stats.signals == ["transcript", "loudness", "motion"]
    for m in ms:
        assert m.found_by == "signals"
        assert gate(m, "audio_evidence").passed is None  # evidence, never a gate
        assert 12 - 1e-6 <= m.end_s - m.start_s <= 45
        assert m.end_s >= m.payoff_s
    assert res.events  # every event is handed on, for the merge


def test_quiet_footage_is_not_dropped(profile) -> None:
    """v1 rejected a window with flat loudness and no motion; v2 keeps it (the ranker judges the content)."""
    script = [(0, "here we go"), (10, "look at this"), (11.5, "no no no"), (30, "okay")]
    res = generate_moments(transcript(script), profile, loudness_db=[-30.0] * 60)
    assert len(res.moments) == 1
    assert "loudness spread 0.0 dB" in gate(res.moments[0], "audio_evidence").detail


def test_a_strong_reaction_without_a_trigger_is_a_hint(profile) -> None:
    script = [(float(t), "we keep talking about the plan") for t in range(0, 40, 2)]
    script += [(41.0, "hahaha hahaha"), (43.0, "that was so dumb")]
    res = generate_moments(transcript(script, duration_s=60), profile)
    assert res.stats.hints == 1
    (m,) = res.moments
    assert m.trigger is None
    assert m.start_s <= 41.0 - 6  # room for the setup before the laugh
    assert "reaction only" in gate(m, "signal_pair").detail


def test_no_reaction_means_no_signal_candidate(profile) -> None:
    script = [(0, "driving along"), (10, "look at this"), (11.1, "we keep driving down the road here")]
    script += [(13.6, "and then we turn"), (15.0, "left"), (40, "okay")]
    res = generate_moments(transcript(script), profile)
    assert res.moments == []
    assert res.stats.unpaired_triggers >= 1


def test_a_cue_phrase_is_never_its_own_reaction() -> None:
    cue = Event("trigger", "cue", 10.0, 11.0, 1.0, "what is that")
    same = Event("reaction", "exclaim", 10.0, 11.0, 2.5, "what is that")
    later = Event("reaction", "exclaim", 11.2, 11.5, 2.5, "no")
    assert not pairs_with(cue, same, 3, 0.5)
    assert pairs_with(cue, later, 3, 0.5)
    motion_trig = Event("trigger", "motion", 8.0, 10.0, 5.0, "z 5")
    assert pairs_with(motion_trig, Event("reaction", "loud", 7.6, 8.6, 3.0, "z 3"), 3, 0.5)
    assert not pairs_with(motion_trig, Event("reaction", "loud", 13.1, 14.0, 3.0, "z 3"), 3, 0.5)


def test_fit_window_keeps_the_payoff_end_and_word_edges(profile) -> None:
    rules = profile.window
    assert fit_window(97.0, 200.0, [], rules, 3600) == (155.0, 200.0)  # 45 s cap keeps the end (payoff)
    assert fit_window(100.0, 104.0, [], rules, 3600) == (92.0, 104.0)  # 12 s minimum adds setup before
    assert fit_window(2.0, 6.0, [], rules, 10.0) is None  # source too short
    words = words_at([(96.8, "watch")])  # 96.8-97.1 spans 97.0
    assert fit_window(97.0, 115.0, words, rules, 3600)[0] == 96.8


def test_overlaps_keep_the_stronger_and_the_cap_applies(profile) -> None:
    script = []
    for k in range(60):
        base = 30.0 * k
        script += [(base, "okay"), (base + 10, "look at this"), (base + 11.2, "no no no"), (base + 12.5, "oh my god")]
    res = generate_moments(transcript(script, duration_s=1800.0), profile)
    ms = res.moments
    assert len(ms) == profile.gates.max_candidates
    for i, a in enumerate(ms):
        for b in ms[i + 1 :]:
            inter = min(a.end_s, b.end_s) - max(a.start_s, b.start_s)
            assert inter <= 0.5 * min(a.end_s - a.start_s, b.end_s - b.start_s) + 1e-6


def test_timestamped_lines() -> None:
    words = words_at([(10, "look at this"), (12, "no no")])
    assert timestamped_lines(words, 9, 14) == "[10.0] look at this\n[12.0] no no"
    assert timestamped_lines(words, 20, 30) == "(no speech)"
