"""Helpers for moment-selection tests: synthetic transcripts, signal arrays and fake responders for the moments-v2
tools (`propose_moments`, `score_moments`, `judge_frames`)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

import httpx2

from clipforge.rank import content_pass, moment_score, vision
from clipforge.rank.profile import MomentProfile, load_profile
from clipforge.schemas import Segment, Transcript, Word
from tests.rank.anthropic_mock import tool_use

WORD_S = 0.3
WORD_GAP_S = 0.05
RUBRIC = ("hook", "arc", "arousal", "sendability", "clarity", "density")
GOOD_SCORES = {"hook": 8, "arc": 7, "arousal": 8, "sendability": 7, "clarity": 7, "density": 6}


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


# A calm stretch, a blunder at ~30 s (motion spike + "no no no"), calm, a laugh moment at ~65 s.
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


def text_of(body: dict[str, Any]) -> str:
    content = body["messages"][0]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def tool_of(body: dict[str, Any]) -> str | None:
    tools = body.get("tools") or []
    return tools[0]["name"] if tools else None


CANDIDATE_RE = re.compile(
    r'<candidate id="([^"]+)" start_s="([\d.]+)" end_s="([\d.]+)" beat_s="([\d.]+)" found_by="(\w+)">'
)
SEGMENT_RE = re.compile(r"Transcript from ([\d.]+) s to ([\d.]+) s:")


def candidates_in(body: dict[str, Any]) -> list[tuple[str, float, float, float]]:
    """(id, start, end, beat) of every candidate in a score_moments request."""
    return [(m[1], float(m[2]), float(m[3]), float(m[4])) for m in CANDIDATE_RE.finditer(text_of(body))]


def found_by_in(body: dict[str, Any]) -> dict[str, str]:
    return {m[1]: m[5] for m in CANDIDATE_RE.finditer(text_of(body))}


def segment_of(body: dict[str, Any]) -> tuple[float, float]:
    m = SEGMENT_RE.search(text_of(body))
    assert m is not None, "not a propose_moments request"
    return float(m[1]), float(m[2])


def moment_item(cid: str, start: float, end: float, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "candidate_id": cid,
        "archetype": "blunder",
        **GOOD_SCORES,
        "has_trigger": True,
        "has_payoff": True,
        "start_s": start,
        "end_s": end,
        "payoff_s": max(start, end - 1.0),
        "brief_ok": True,
        "skip_flags": [],
        "review_flags": [],
        "reason": "Sells his car for a dollar by mistake and screams.",
    }
    item.update(overrides)
    return item


def proposal(start: float, end: float, **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "start_s": start,
        "end_s": end,
        "trigger_s": start + 1.0,
        "payoff_s": end - 1.0,
        "archetype": "blunder",
        "confidence": 0.8,
        "reason": "He sells his car for a dollar by mistake.",
    }
    item.update(overrides)
    return item


def verdict(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "visible_event": True,
        "visible_payoff": True,
        "reaction_visible": True,
        "scene": "gameplay",
        "spoiler_risk": False,
        "graphic_risk": False,
        "setup_clear": True,
        **GOOD_SCORES,
        "note": "A menu purchase, then his face drops.",
    }
    item.update(overrides)
    return item


Responder = Callable[[dict[str, Any]], httpx2.Response]


def echo_moments(
    per_candidate: Callable[[str, float, float, float], dict[str, Any]] | None = None,
) -> Responder:
    """score_moments responder: one item per candidate in the request, keeping the generated window."""

    def respond(body: dict[str, Any]) -> httpx2.Response:
        items = []
        for cid, start, end, beat in candidates_in(body):
            extra = per_candidate(cid, start, end, beat) if per_candidate else {}
            items.append(moment_item(cid, start, end, **extra))
        return tool_use(moment_score.TOOL_NAME, {"scores": items})

    return respond


def propose(proposals: Sequence[dict[str, Any]] = ()) -> Responder:
    """propose_moments responder: the given proposals that start inside the requested segment."""

    def respond(body: dict[str, Any]) -> httpx2.Response:
        lo, hi = segment_of(body)
        items = [p for p in proposals if lo <= p["start_s"] < hi]
        return tool_use(content_pass.TOOL_NAME, {"moments": items})

    return respond


def judge(**overrides: Any) -> Responder:
    def respond(body: dict[str, Any]) -> httpx2.Response:
        return tool_use(vision.TOOL_NAME, verdict(**overrides))

    return respond


def moment_tools(
    *,
    proposals: Sequence[dict[str, Any]] = (),
    per_candidate: Callable[[str, float, float, float], dict[str, Any]] | None = None,
    frames: Responder | None = None,
    other: Responder | None = None,
) -> Responder:
    """One responder for every moments-v2 tool (and `other` for anything else, e.g. Sonnet copy)."""
    by_tool = {
        content_pass.TOOL_NAME: propose(proposals),
        moment_score.TOOL_NAME: echo_moments(per_candidate),
        vision.TOOL_NAME: frames or judge(),
    }

    def respond(body: dict[str, Any]) -> httpx2.Response:
        name = tool_of(body)
        if name in by_tool:
            return by_tool[name](body)
        if other is None:
            raise AssertionError(f"unexpected request for tool {name!r}")
        return other(body)

    return respond
