"""Chunk-boundary safety (moments-v2 item 7): a moment that straddles a Groq audio-chunk boundary, a content-pass
segment edge or a ranker batch boundary comes out as ONE intact candidate with its setup and payoff.

Pipeline facts these tests pin:
- Groq chunks overlap (45 s) and are stitched on one global timeline at a seam inside the overlap; each word once.
- Signals, the content pass and candidate windows all run on that global timeline (never per chunk).
- Ranker batches are by candidate; each candidate carries 30 s of transcript either side.
- A window whose payoff runs past it is extended, never truncated.
"""

from __future__ import annotations

from typing import Any

from clipforge.rank.claude_client import ClaudeClient
from clipforge.rank.moment_score import BATCH_SIZE, rank_moments
from clipforge.rank.moments import text_in
from clipforge.schemas import Platform, Segment, Transcript, Word
from clipforge.transcribe.chunker import ChunkResult, merge_chunks, plan_chunks
from tests.rank.anthropic_mock import FakeAnthropic
from tests.rank.moment_fakes import (
    candidates_in,
    gta_profile,
    moment_item,
    moment_tools,
    proposal,
    text_of,
    tool_of,
    words_at,
)
from tests.support.factories import campaign_in

SETUP = "so this button should buy the car"
TRIGGER = "no no no"
PAYOFF = "I just sold my car for one dollar"


def filler(start: float, end: float, every: float = 2.5) -> list[tuple[float, str]]:
    out = []
    t = start
    while t < end:
        out.append((t, "and we keep on driving down this road"))
        t += every
    return out


def chunk_view(truth: list[Word], plan) -> list[Word]:
    """What one Groq chunk hears: every word that lies wholly inside it, in chunk-local time."""
    return [
        Word(text=w.text, start_s=round(w.start_s - plan.start_s, 3), end_s=round(w.end_s - plan.start_s, 3))
        for w in truth
        if plan.start_s <= w.start_s and w.end_s <= plan.end_s
    ]


def test_moment_straddling_a_groq_chunk_boundary_is_one_intact_candidate(
    fake: FakeAnthropic, client: ClaudeClient
) -> None:
    duration = 3600.0
    plans = plan_chunks(duration, silences=[(1776.0, 1778.5)])
    first, second = plans
    boundary = first.end_s  # chunk 0 ends here; chunk 1 started 45 s earlier
    assert second.start_s < boundary
    assert first.seam_after_s == 1777.25  # the seam sits in the silence, not mid-word

    # The moment runs from before the end of chunk 0 to after it: setup, trigger and payoff on both sides.
    script = filler(0, boundary - 14) + [
        (boundary - 10.0, SETUP),
        (boundary - 3.0, TRIGGER),
        (boundary - 1.0, "what did I just do"),
        (boundary + 2.0, PAYOFF),
        (boundary + 6.0, "hahaha I'm so dumb"),
    ]
    script += filler(boundary + 12, duration - 5)
    truth = words_at(script)
    results = [ChunkResult(p, chunk_view(truth, p)) for p in plans]
    words, _ = merge_chunks(results)
    assert [w.text for w in words] == [w.text for w in truth]  # every word once, in order, global times
    assert [w.start_s for w in words] == [w.start_s for w in truth]

    tr = Transcript(source_id=7, provider="groq", model="whisper-large-v3-turbo", words=words,
                    segments=[Segment(text=w.text, start_s=w.start_s, end_s=w.end_s) for w in words],
                    duration_s=duration)  # fmt: skip
    setup_t, payoff_end = boundary - 10.0, boundary + 8.0
    fake.default = moment_tools(
        proposals=[proposal(setup_t, payoff_end, trigger_s=boundary - 3.0, payoff_s=payoff_end - 0.5)],
        per_candidate=lambda cid, s, e, beat: {"start_s": s, "end_s": e, "payoff_s": e - 0.5},
    )
    result = rank_moments(tr, campaign_in(), [Platform.TIKTOK], gta_profile(), client=client)
    near = [c for c in result.candidates if c.start_s < boundary + 30 and c.end_s > boundary - 30]
    assert len(near) == 1  # one candidate, not two halves
    (c,) = near
    assert c.start_s <= setup_t + 0.05
    assert c.end_s >= boundary + 6.0
    text = text_in(words, c.start_s, c.end_s)
    assert SETUP in text
    assert TRIGGER in text
    assert PAYOFF in text
    # the content pass saw the moment whole inside one segment (segments overlap by 2 min)
    proposals = [b for b in fake.requests if tool_of(b) == "propose_moments"]
    assert any(SETUP in text_of(b) and PAYOFF in text_of(b) for b in proposals)


def test_moment_at_a_ranker_batch_boundary_keeps_its_setup_and_payoff(
    fake: FakeAnthropic, client: ClaudeClient
) -> None:
    """Nine candidates, so the ninth lands in the second scoring batch. Its setup sits in the window of the eighth
    (scored in the first batch) and its payoff runs past its own window: it still comes out whole."""
    profile = gta_profile()
    duration = 900.0
    script = filler(0, 497) + filler(560, 600)  # no filler words under the target's setup and payoff
    starts = [40.0 + 60.0 * k for k in range(9)]
    for k, s in enumerate(starts[:8]):
        script += [(s, f"moment number {k} starts here"), (s + 8, "no way bro"), (s + 12, "hahaha")]
    target = starts[8]  # 520.0
    setup_t = target - 18.0  # 502 s: outside the target's proposed window, inside its 30 s of context
    script += [(setup_t, SETUP), (target, TRIGGER), (target + 14.0, PAYOFF), (target + 18.0, "that was so dumb")]
    script.sort()
    from tests.rank.moment_fakes import transcript

    tr = transcript(script, duration_s=duration)
    props = [proposal(s, s + 15.0, confidence=round(0.95 - 0.01 * k, 2)) for k, s in enumerate(starts)]
    # the target is proposed narrowly (trigger to the start of the payoff): ranked 9th by confidence
    props[8] = proposal(target, target + 15.0, confidence=0.86, trigger_s=target, payoff_s=target + 14.0)

    def per(cid: str, s: float, e: float, beat: float) -> dict[str, Any]:
        if s < target + 15 and e > setup_t:  # any candidate on this moment: the ranker opens on the setup (from the
            # context) and ends after the payoff
            return {"start_s": setup_t, "end_s": target + 16.0, "payoff_s": target + 19.5}
        return {}

    fake.default = moment_tools(proposals=props, per_candidate=per)
    result = rank_moments(tr, campaign_in(), [Platform.TIKTOK], profile, client=client, top_n=20)

    batches = [b for b in fake.requests if tool_of(b) == "score_moments"]
    assert len(batches) >= 2
    first_ids = [cid for cid, *_ in candidates_in(batches[0])]
    second = batches[1]
    assert len(first_ids) == BATCH_SIZE
    target_ids = [cid for cid, s, e, beat in candidates_in(second) if abs(beat - target) < 0.5]
    assert target_ids  # the target is scored in the second batch...
    assert SETUP in text_of(second)  # ...and its setup reaches it as context, though another candidate holds it

    (c,) = [c for c in result.candidates if c.start_s <= target <= c.end_s]
    assert c.start_s <= setup_t + 0.05
    assert c.end_s >= target + 19.5  # extended to the payoff, not cut at the proposed end
    assert c.end_s - c.start_s <= profile.window.max_s
    text = text_in(tr.words, c.start_s, c.end_s)
    assert SETUP in text
    assert PAYOFF in text
    assert "that was so dumb" in text
    assert any("extended to the payoff" in g.detail for g in c.moment.gates if g.name == "window")


def test_moment_cut_by_a_content_segment_edge_is_kept_whole(fake: FakeAnthropic, client: ClaudeClient) -> None:
    """Segment 0 ends at 720 s and sees the moment cut there; segment 1 (from 600 s) sees it whole."""
    tr_script = filler(0, 700) + [(705.0, SETUP), (712.0, TRIGGER), (722.0, PAYOFF), (730.0, "hahaha no")]
    tr_script += filler(740, 1400)
    tr_script.sort()
    from tests.rank.moment_fakes import transcript

    tr = transcript(tr_script, duration_s=1400)
    fake.default = moment_tools(proposals=[proposal(705.0, 733.0, payoff_s=732.0)])
    # the responder only returns proposals that START inside the requested segment, so both segment 0 (cut at its
    # end, clamped to 705-720) and segment 1 (whole) propose it
    result = rank_moments(tr, campaign_in(), [Platform.TIKTOK], gta_profile(), client=client)
    run = result.moment_run
    near = [m for m in run.content.moments if m.start_s < 740 and m.end_s > 700]
    assert len(near) == 1
    (m,) = near
    assert (m.start_s, m.end_s) == (705.0, 733.0)
    assert not m.edge
    assert m.segments == (0, 1)
    (c,) = [c for c in result.candidates if c.start_s < 740 and c.end_s > 700]
    assert SETUP in text_in(tr.words, c.start_s, c.end_s)
    assert PAYOFF in text_in(tr.words, c.start_s, c.end_s)


def test_moment_item_default_keeps_window(fake: FakeAnthropic) -> None:
    item = moment_item("x", 10.0, 30.0)
    assert item["payoff_s"] == 29.0
