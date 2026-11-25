"""Moment candidates: trigger+reaction gate, window rules, dead-footage gate, overlap and cap (pure)."""

from __future__ import annotations

import pytest

from clipforge.rank.moments import generate_moments, pairs_with, timestamped_lines, window_for
from clipforge.rank.signals import Event
from tests.rank.moment_fakes import REACTION_SCRIPT, gta_profile, loudness, motion, transcript, words_at


@pytest.fixture(scope="module")
def profile():
    return gta_profile()


def gate(m, name):
    return next(g for g in m.gates if g.name == name)


def test_reaction_script_gives_the_two_moments(profile) -> None:
    tr = transcript(REACTION_SCRIPT)
    ms, stats = generate_moments(
        tr, profile, loudness_db=loudness(spikes={31: -8.0, 32: -10.0, 66: -9.0}), motion=motion(spikes={30.0: 25.0})
    )
    assert [m.moment_id for m in ms] == ["7:m26400", "7:m64000"]
    blunder, laugh = ms
    assert {r.kind for r in blunder.reactions} == {"exclaim", "loud"}
    assert {r.kind for r in laugh.reactions} == {"laugh", "loud"}
    assert stats.signals == ["transcript", "loudness", "motion"]
    for m in ms:
        assert gate(m, "trigger_reaction").passed is True
        assert gate(m, "dead_footage").passed is True
        lead = m.trigger.t - m.start_s
        assert 2 - 1e-6 <= lead <= 5 + 1e-6  # start 2-5 s before the trigger
        assert 12 - 1e-6 <= m.end_s - m.start_s <= 45  # min 12 s, hard cap 45 s
        assert m.end_s >= m.payoff_s  # ends after the payoff
        # ...and right after it, unless the 12 s minimum needed more
        assert m.end_s - m.payoff_s <= profile.window.post_roll_s + 1.0 or m.end_s - m.start_s <= 12 + 0.5


def test_no_reaction_within_3s_means_no_candidate(profile) -> None:
    # The cue ends at 11.0 s; talk continues with no pause long enough for a silence beat; "no" comes 4.4 s later.
    script = [
        (0, "driving along"),
        (10, "look at this"),
        (11.1, "we keep driving down the road here"),
        (13.6, "and then we turn"),
        (15.0, "left"),
        (15.4, "no no no"),
        (40, "okay"),
    ]
    ms, stats = generate_moments(transcript(script), profile)
    assert ms == []
    assert stats.unpaired_triggers >= 1


def test_reaction_with_no_trigger_means_no_candidate(profile) -> None:
    # Continuous speech (no gap long enough for a silence beat), no cue, no media signals: nothing triggers.
    script = [(float(t), "we keep talking about the plan") for t in range(0, 60, 2)]
    script.append((61.0, "bro"))
    ms, _ = generate_moments(transcript(script), profile)
    assert ms == []


def test_dead_footage_gate(profile) -> None:
    # A triggered moment in footage with almost no loudness change is dead unless something moves on screen.
    script = [(0, "here we go"), (10, "look at this"), (11.5, "no no no"), (30, "okay")]
    flat = [-30.0] * 60
    ms, stats = generate_moments(transcript(script), profile, loudness_db=flat)
    assert ms == []
    assert stats.rejected_dead >= 1
    ms, _ = generate_moments(transcript(script), profile, loudness_db=flat, motion=motion(60, spikes={10.0: 30.0}))
    assert len(ms) >= 1
    assert "motion spike yes" in gate(ms[0], "dead_footage").detail
    ms, _ = generate_moments(transcript(script), profile)
    assert gate(ms[0], "dead_footage").passed is None  # no loudness data: not evaluated, not rejected


def test_a_cue_phrase_is_never_its_own_reaction(profile) -> None:
    cue = Event("trigger", "cue", 10.0, 11.0, 1.0, "what is that")
    same = Event("reaction", "exclaim", 10.0, 11.0, 2.5, "what is that")
    later = Event("reaction", "exclaim", 11.2, 11.5, 2.5, "no")
    assert not pairs_with(cue, same, 3, 0.5)
    assert pairs_with(cue, later, 3, 0.5)
    motion_trig = Event("trigger", "motion", 8.0, 10.0, 5.0, "z 5")
    assert pairs_with(motion_trig, Event("reaction", "loud", 7.6, 8.6, 3.0, "z 3"), 3, 0.5)  # may lead by 0.5 s
    assert not pairs_with(motion_trig, Event("reaction", "loud", 13.1, 14.0, 3.0, "z 3"), 3, 0.5)


def test_window_hard_cap_and_minimum(profile) -> None:
    trig = Event("trigger", "motion", 100.0, 100.0, 5.0, "")
    assert window_for(trig, 200.0, [], profile, 3600) == (97.0, 142.0)  # 45 s hard cap
    start, end = window_for(trig, 101.0, [], profile, 3600)
    assert start == 95.0
    assert end == pytest.approx(107.0)
    assert window_for(Event("trigger", "motion", 5.0, 5.0, 5.0, ""), 6.0, [], profile, 10.0) is None  # source too short


def test_window_start_snaps_to_a_word_edge(profile) -> None:
    words = words_at([(95.5, "so this is it")])  # "so" 95.5-95.8, "this" 95.85-96.15 ...
    trig = Event("trigger", "motion", 100.0, 100.0, 5.0, "")
    start, _ = window_for(trig, 115.0, words, profile, 3600)
    assert start == 97.0  # 97.0 is in no word, kept
    words = words_at([(96.8, "watch")])  # 96.8-97.1 spans 97.0
    start, _ = window_for(trig, 115.0, words, profile, 3600)
    assert start == 96.8  # moved back to the word start (still within 5 s)


def test_overlaps_keep_the_stronger_and_the_cap_applies(profile) -> None:
    script = []
    for k in range(60):
        base = 30.0 * k
        script += [(base, "okay"), (base + 10, "look at this"), (base + 11.2, "no no no"), (base + 12.5, "oh my god")]
    ms, stats = generate_moments(transcript(script, duration_s=1800.0), profile)
    assert len(ms) == profile.gates.max_candidates
    for i, a in enumerate(ms):
        for b in ms[i + 1 :]:
            inter = min(a.end_s, b.end_s) - max(a.start_s, b.start_s)
            assert inter <= 0.5 * min(a.end_s - a.start_s, b.end_s - b.start_s) + 1e-6


def test_timestamped_lines(profile) -> None:
    words = words_at([(10, "look at this"), (12, "no no")])
    assert timestamped_lines(words, 9, 14) == "[10.0] look at this\n[12.0] no no"
    assert timestamped_lines(words, 20, 30) == "(no speech)"
