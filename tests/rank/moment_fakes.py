"""Helpers for moment-selection tests: synthetic transcripts, signal arrays and a fake `score_moments` responder."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx2

from clipforge.rank.moment_score import TOOL_NAME
from clipforge.rank.profile import MomentProfile, load_profile
from clipforge.schemas import Segment, Transcript, Word
from tests.rank.anthropic_mock import tool_use

WORD_S = 0.3
WORD_GAP_S = 0.05


def words_at(script: Sequence[tuple[float, str]]) -> list[Word]:
    """Each (start, text) becomes words spaced WORD_S + WORD_GAP_S apart from `start`."""
    out: list[Word] = []
    for start, text in script:
        t = start
        for token in text.split():
            out.append(Word(text=token, start_s=round(t, 3), end_s=round(t + WORD_S, 3)))
            t += WORD_S + WORD_GAP_S
    return out


def transcript(script: Sequence[tuple[float, str]], *, source_id: int = 7, duration_s: float = 90.0) -> Transcript:
    words = words_at(script)
    segments = [Segment(text=w.text, start_s=w.start_s, end_s=w.end_s) for w in words]
    return Transcript(
        source_id=source_id, provider="groq", model="whisper-large-v3-turbo", words=words, segments=segments,
        duration_s=duration_s,
    )  # fmt: skip


# A calm stretch, a blunder at ~30 s (motion spike + "no no no"), calm, a laugh moment at ~70 s.
REACTION_SCRIPT: list[tuple[float, str]] = [
    (1.0, "okay so we are just driving to the shop now"),
    (6.0, "the weather is nice today in the game"),
    (12.0, "I think we go left here and then park"),
    (18.0, "let me open the menu real quick"),
    (24.0, "so this button should buy the car"),
    (31.0, "no no no"),
    (32.5, "what did I just do"),
    (35.0, "I just sold my car for one dollar"),
    (40.0, "okay okay let's keep going"),
    (48.0, "we are driving again down the road"),
    (56.0, "this road is long and quiet"),
    (64.0, "look at this guy"),
    (66.0, "hahaha"),
    (67.0, "he is dancing on the car"),
    (72.0, "alright moving on"),
]


def loudness(n: int = 90, base: float = -30.0, spikes: dict[int, float] | None = None) -> list[float]:
    """Per-second dB with a small natural wiggle and loud seconds at `spikes` {second: dB}."""
    out = [base + (1.5 if k % 2 else -1.5) for k in range(n)]
    for k, v in (spikes or {}).items():
        out[k] = v
    return out


def motion(n: float = 90.0, step: float = 2.0, base: float = 2.0, spikes: dict[float, float] | None = None) -> list:
    """Keyframe-like samples every `step` s with spikes at {time: diff}."""
    out = []
    t = 0.0
    k = 0
    while t <= n:
        out.append((t, (spikes or {}).get(t, base + (0.3 if k % 2 else -0.3))))
        t += step
        k += 1
    return out


def gta_profile() -> MomentProfile:
    return load_profile("gta6-streamer")


CANDIDATE_RE = re.compile(r'<candidate id="([^"]+)" start_s="([\d.]+)" end_s="([\d.]+)" trigger_s="([\d.]+)">')


def candidates_in(body: dict[str, Any]) -> list[tuple[str, float, float, float]]:
    text = body["messages"][0]["content"]
    return [(m[1], float(m[2]), float(m[3]), float(m[4])) for m in CANDIDATE_RE.finditer(text)]


def moment_item(cid: str, start: float, end: float, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "candidate_id": cid,
        "archetype": "blunder",
        "reaction": 8,
        "event": 7,
        "setup": 7,
        "absurdity": 8,
        "start_s": start,
        "end_s": end,
        "brief_ok": True,
        "skip_flags": [],
        "review_flags": [],
        "reason": "Sells his car for a dollar by mistake and screams.",
    }
    item.update(overrides)
    return item


def echo_moments(
    per_candidate: Callable[[str, float, float, float], dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], httpx2.Response]:
    """Responder: one item per candidate in the request, keeping the generated window unless overridden."""

    def respond(body: dict[str, Any]) -> httpx2.Response:
        items = []
        for cid, start, end, trig in candidates_in(body):
            extra = per_candidate(cid, start, end, trig) if per_candidate else {}
            items.append(moment_item(cid, start, end, **extra))
        return tool_use(TOOL_NAME, {"scores": items})

    return respond
