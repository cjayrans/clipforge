"""Moment signals (pure): z-scores, transcript tokens, loudness, motion and silence beats."""

from __future__ import annotations

import pytest

from clipforge.rank.signals import (
    MIN_SIGMA_DB,
    all_events,
    loudness_events,
    motion_events,
    rolling_z,
    sensitive_hits,
    silence_beats,
    transcript_events,
)
from tests.rank.moment_fakes import gta_profile, loudness, motion, words_at


@pytest.fixture(scope="module")
def profile():
    return gta_profile()


def kinds(events):
    return [(e.kind, e.detail) for e in events]


def test_rolling_z_is_robust_and_floored() -> None:
    values = [-30.0] * 30 + [-10.0] + [-30.0] * 5
    z = rolling_z(values, [float(i) for i in range(len(values))], 60, MIN_SIGMA_DB)
    assert z[30] == pytest.approx(20 / MIN_SIGMA_DB)  # flat baseline: MAD is 0, sigma falls back to the floor
    assert max(abs(v) for i, v in enumerate(z) if i != 30) < 1e-9  # one spike does not move the median


def test_rolling_z_uses_only_the_trailing_baseline() -> None:
    values = [-50.0] * 100 + [-20.0] * 100 + [-20.0]
    times = [float(i) for i in range(len(values))]
    z = rolling_z(values, times, 60, MIN_SIGMA_DB)
    assert z[100] > 5  # louder than the last minute
    assert abs(z[-1]) < 1e-9  # a minute later the new level is the baseline


def test_exclamations_count_only_when_stressed(profile) -> None:
    words = words_at([(0, "what do you want to eat"), (5, "what?"), (8, "no no no"), (12, "bro")])
    ev = transcript_events(words, profile.lexicon)
    exclaims = [e for e in ev if e.kind == "exclaim"]
    assert [e.t for e in exclaims][0] == 5  # the plain question word at 0 s is not a reaction
    assert [e.detail for e in exclaims] == ["what", "no", "no", "no", "bro"]
    assert all(e.role == "reaction" for e in exclaims)


def test_phrases_laughter_cues_and_chat(profile) -> None:
    words = words_at([(0, "oh my god"), (3, "HAHAHA"), (5, "look at this guy"), (9, "chat did you see that")])
    ev = transcript_events(words, profile.lexicon)
    assert ("exclaim", "oh my god") in kinds(ev)
    assert ("laugh", "hahaha") in kinds(ev)
    assert [d for k, d in kinds(ev) if k == "cue" and d.startswith("look")] == ["look at"]  # one event per instant
    assert ("chat", "chat") in kinds(ev)
    assert ("cue", "did you see") in kinds(ev)
    assert {e.role for e in ev if e.kind in ("cue", "chat")} == {"trigger"}


def test_loud_with_speech_is_a_reaction_without_is_an_onset(profile) -> None:
    words = words_at([(40, "no way")])
    db = loudness(spikes={20: -5.0, 40: -5.0})
    ev = loudness_events(db, words, profile.signals)
    assert [(e.role, e.kind, e.t) for e in ev] == [("trigger", "onset", 20.0), ("reaction", "loud", 40.0)]


def test_motion_spike_spans_from_the_previous_keyframe(profile) -> None:
    ev = motion_events(motion(spikes={30.0: 25.0}), profile.signals)
    assert [(e.kind, e.t, e.end_t) for e in ev] == [("motion", 28.0, 30.0)]


def test_tiny_motion_spikes_are_ignored(profile) -> None:
    samples = [(float(t), 0.1) for t in range(0, 60, 2)] + [(60.0, 3.0)]
    assert motion_events(samples, profile.signals) == []  # z is huge, but 3 < motion_min_diff


def test_silence_beat_needs_a_reaction_right_after(profile) -> None:
    words = words_at([(0, "okay here we go"), (5, "no no no"), (20, "anyway"), (25, "so yeah")])
    reactions = [e for e in transcript_events(words, profile.lexicon) if e.role == "reaction"]
    beats = silence_beats(words, reactions, profile.signals)
    assert len(beats) == 1
    assert beats[0].t == pytest.approx(1.35)
    assert beats[0].end_t == 5.0


def test_all_events_without_media_uses_the_transcript_only(profile) -> None:
    words = words_at([(0, "look at this"), (2, "no no")])
    ev = all_events(words, profile.lexicon, profile.signals)
    assert {e.kind for e in ev} <= {"cue", "exclaim", "silence_beat", "chat", "laugh"}


def test_sensitive_hits_match_whole_phrases(profile) -> None:
    assert sensitive_hits("we went to the Strip Club, bro", profile.lexicon.sensitive) == ["strip club"]
    assert sensitive_hits("the stripes on the car", profile.lexicon.sensitive) == []
