"""B3: candidate selection (pure): brief filter, ranking, trims, overlap de-duplication, top N."""

from __future__ import annotations

from typing import Any

from clipforge.rank.select import NOT_STANDALONE_PENALTY, select_candidates, text_between
from clipforge.schemas import Window, WindowScore, Word


def win(wid: str, start: float, end: float, prescore: float = 0.0) -> Window:
    return Window(window_id=wid, source_id=3, start_s=start, end_s=end, text=f"text {wid}", prescore=prescore)


def sc(wid: str, score: int, **kw: Any) -> WindowScore:
    data: dict[str, Any] = {
        "window_id": wid,
        "score": score,
        "standalone": True,
        "brief_ok": True,
        "hook_potential": 5,
        "reasons": f"r {wid}",
    }
    data.update(kw)
    return WindowScore.model_validate(data)


def words_every_half_second(until: float) -> list[Word]:
    out, t = [], 0.0
    while t < until:
        out.append(Word(text=f"w{int(t * 2)}", start_s=t, end_s=t + 0.4))
        t += 0.5
    return out


def run(windows, scores, **kw):
    params = {"top_n": 8, "min_clip_s": 10.0, "max_clip_s": 90.0, "prompt_version": "score_windows@1+clip_copy@1"}
    params.update(kw)
    return select_candidates(windows, scores, **params)


def test_ranks_by_score_and_drops_off_brief():
    windows = [win("a", 0, 30), win("b", 100, 130), win("c", 200, 230)]
    picked = run(windows, [sc("a", 60), sc("b", 90, brief_ok=False), sc("c", 70)])
    assert [p.window.window_id for p in picked] == ["c", "a"]
    cand = picked[0].candidate
    assert (cand.source_id, cand.start_s, cand.end_s, cand.score) == (3, 200, 230, 70)
    assert cand.prompt_version == "score_windows@1+clip_copy@1"
    assert cand.copy_ is None
    assert cand.reasons == "r c"


def test_not_standalone_penalty():
    windows = [win("a", 0, 30), win("b", 100, 130)]
    picked = run(windows, [sc("a", 80, standalone=False), sc("b", 80 - NOT_STANDALONE_PENALTY + 1)])
    assert [p.window.window_id for p in picked] == ["b", "a"]
    assert picked[1].effective_score == 80 - NOT_STANDALONE_PENALTY


def test_tie_break_by_hook_then_prescore():
    windows = [win("a", 0, 30, prescore=0.9), win("b", 100, 130, prescore=0.1), win("c", 200, 230, prescore=0.5)]
    picked = run(windows, [sc("a", 50), sc("b", 50, hook_potential=9), sc("c", 50)])
    assert [p.window.window_id for p in picked] == ["b", "a", "c"]


def test_overlapping_windows_deduplicated():
    windows = [win("a", 0, 30), win("b", 5, 35), win("c", 25, 55)]
    picked = run(windows, [sc("a", 90), sc("b", 80), sc("c", 70)])
    # b overlaps a by 25/30; c overlaps a by 5/30.
    assert [p.window.window_id for p in picked] == ["a", "c"]


def test_top_n():
    windows = [win(str(i), i * 100, i * 100 + 30) for i in range(12)]
    assert len(run(windows, [sc(str(i), 50 + i) for i in range(12)], top_n=3)) == 3


def test_trims_snap_to_words_and_text_follows():
    words = words_every_half_second(60)
    picked = run([win("a", 0, 40)], [sc("a", 80, trim_start_s=5.1, trim_end_s=30.05)], words=words)
    cand = picked[0].candidate
    assert cand.start_s == 5.0  # first word starting at or after 5.1 - 0.25
    assert cand.end_s == 29.9  # last word ending at or before 30.05 + 0.25
    assert picked[0].text.split()[0] == "w10"
    assert picked[0].text.split()[-1] == "w59"


def test_trims_outside_window_ignored():
    picked = run([win("a", 10, 40)], [sc("a", 80, trim_start_s=2.0, trim_end_s=99.0)])
    assert (picked[0].candidate.start_s, picked[0].candidate.end_s) == (10, 40)


def test_trim_too_short_falls_back_to_window():
    picked = run([win("a", 0, 30)], [sc("a", 80, trim_start_s=10.0, trim_end_s=15.0)])
    assert (picked[0].candidate.start_s, picked[0].candidate.end_s) == (0, 30)


def test_window_longer_than_max_clip_skipped():
    assert run([win("a", 0, 70)], [sc("a", 80)], max_clip_s=60.0) == []


def test_unknown_score_ids_ignored():
    assert run([win("a", 0, 30)], [sc("nope", 99)]) == []


def test_text_without_words_uses_window_text():
    assert run([win("a", 0, 30)], [sc("a", 80)])[0].text == "text a"


def test_text_between_midpoints():
    words = [Word(text="a", start_s=0, end_s=1), Word(text="b", start_s=1, end_s=2), Word(text="c", start_s=2, end_s=3)]
    # midpoints 0.5, 1.5, 2.5
    assert text_between(words, 0.5, 2.5) == "a b c"
    assert text_between(words, 0.6, 2.4) == "b"
