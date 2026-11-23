"""Pick candidates from scored windows (pure): filter, rank, apply trims, de-duplicate overlaps, top N."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.schemas import Candidate, Window, WindowScore, Word

NOT_STANDALONE_PENALTY = 15  # score points
MAX_OVERLAP = 0.5  # share of the shorter clip
TRIM_SNAP_S = 0.25  # how far a trim may move to reach a word boundary


@dataclass(frozen=True)
class Selection:
    candidate: Candidate
    window: Window
    text: str  # transcript text of the (trimmed) candidate range
    effective_score: int


def text_between(words: Sequence[Word], start_s: float, end_s: float) -> str:
    """Words whose midpoint lies inside [start_s, end_s]."""
    return " ".join(w.text.strip() for w in words if start_s <= (w.start_s + w.end_s) / 2 <= end_s).strip()


def _snap_trims(window: Window, score: WindowScore, words: Sequence[Word]) -> tuple[float, float]:
    """Apply the model's trims if they fall inside the window; snap them outward to word boundaries."""
    start, end = window.start_s, window.end_s
    inside = [w for w in words if window.start_s <= w.start_s and w.end_s <= window.end_s]
    if score.trim_start_s is not None and window.start_s < score.trim_start_s < window.end_s:
        start = score.trim_start_s
        firsts = [w.start_s for w in inside if w.start_s >= start - TRIM_SNAP_S]
        start = min(firsts) if firsts else start
    if score.trim_end_s is not None and window.start_s < score.trim_end_s < window.end_s:
        end = score.trim_end_s
        lasts = [w.end_s for w in inside if w.end_s <= end + TRIM_SNAP_S]
        end = max(lasts) if lasts else end
    return start, end


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    shorter = min(a[1] - a[0], b[1] - b[0])
    return max(inter, 0.0) / shorter if shorter > 0 else 0.0


def select_candidates(
    windows: Sequence[Window],
    scores: Sequence[WindowScore],
    *,
    words: Sequence[Word] = (),
    top_n: int,
    min_clip_s: float,
    max_clip_s: float,
    prompt_version: str,
) -> list[Selection]:
    """Best `top_n` candidates, best first. Drops `brief_ok=False`; non-standalone windows lose
    `NOT_STANDALONE_PENALTY` points; a trim that would leave the clip outside [min_clip_s, max_clip_s] is ignored;
    candidates overlapping a better one by more than `MAX_OVERLAP` are dropped."""
    by_id = {w.window_id: w for w in windows}
    ranked: list[tuple[int, WindowScore, Window]] = []
    for s in scores:
        w = by_id.get(s.window_id)
        if w is None or not s.brief_ok:
            continue
        effective = s.score - (0 if s.standalone else NOT_STANDALONE_PENALTY)
        ranked.append((effective, s, w))
    ranked.sort(key=lambda t: (-t[0], -t[1].hook_potential, -t[2].prescore, t[2].start_s, t[2].end_s))

    out: list[Selection] = []
    for effective, s, w in ranked:
        if len(out) >= top_n:
            break
        start, end = _snap_trims(w, s, words)
        if not min_clip_s <= end - start <= max_clip_s:
            start, end = w.start_s, w.end_s
        if not min_clip_s <= end - start <= max_clip_s:
            continue
        if any(_overlap((start, end), (o.candidate.start_s, o.candidate.end_s)) > MAX_OVERLAP for o in out):
            continue
        text = text_between(words, start, end) if words else w.text
        candidate = Candidate(
            source_id=w.source_id,
            start_s=round(start, 3),
            end_s=round(end, 3),
            score=s.score,
            standalone=s.standalone,
            brief_ok=s.brief_ok,
            reasons=s.reasons,
            prompt_version=prompt_version,
        )
        out.append(Selection(candidate=candidate, window=w, text=text or w.text, effective_score=effective))
    return out


# --------------------------------------------------------------------------------------------------
# "Shorter" (bot button, ARCHITECTURE §4.6): trim 20% from the weaker end using word timestamps
# --------------------------------------------------------------------------------------------------

SHORTER_FRACTION = 0.2
SENTENCE_SNAP_S = 3.0  # a cut may move up to this much further inward to land on a sentence boundary
_SENTENCE_END = (".", "?", "!", "…")


def _ends_sentence(word: Word) -> bool:
    return word.text.rstrip().rstrip("\"')]").endswith(_SENTENCE_END)


def shorter_range(
    words: Sequence[Word],
    start_s: float,
    end_s: float,
    *,
    min_clip_s: float,
    fraction: float = SHORTER_FRACTION,
) -> tuple[float, float]:
    """New (start, end) at most `1 - fraction` of the current length, cut from the weaker end (pure).

    Both cuts are tried: dropping the tail (new end on a word end) and dropping the head (new start on a word start).
    Each snaps inward to a sentence boundary when one lies within `SENTENCE_SNAP_S`. The weaker end is the one whose
    cut part holds fewer words (less speech is lost); a tie trims the end. Raises ValueError if the result would be
    shorter than `min_clip_s`.
    """
    if not 0 < fraction < 1:
        raise ValueError("fraction must be between 0 and 1")
    if end_s <= start_s:
        raise ValueError("end must be after start")
    target = (end_s - start_s) * (1 - fraction)
    if target < min_clip_s:
        raise ValueError(f"clip is {end_s - start_s:.1f}s; 20% shorter would be under the {min_clip_s:.0f}s minimum")
    inside = [w for w in words if start_s <= (w.start_s + w.end_s) / 2 <= end_s]

    # Option A: cut the tail.
    limit = start_s + target
    ends = [w for w in inside if w.end_s <= limit and w.end_s - start_s >= min_clip_s]
    snapped = [w.end_s for w in ends if _ends_sentence(w) and w.end_s >= limit - SENTENCE_SNAP_S]
    new_end = max(snapped) if snapped else (max(w.end_s for w in ends) if ends else limit)

    # Option B: cut the head (a new start follows a sentence end when one is close).
    limit = end_s - target
    starts = [(i, w) for i, w in enumerate(inside) if w.start_s >= limit and end_s - w.start_s >= min_clip_s]
    snapped = [
        w.start_s for i, w in starts if i > 0 and _ends_sentence(inside[i - 1]) and w.start_s <= limit + SENTENCE_SNAP_S
    ]
    new_start = min(snapped) if snapped else (min(w.start_s for _, w in starts) if starts else limit)

    lost_tail = sum(1 for w in inside if (w.start_s + w.end_s) / 2 > new_end)
    lost_head = sum(1 for w in inside if (w.start_s + w.end_s) / 2 < new_start)
    if lost_head < lost_tail:
        return min(max(round(new_start, 3), start_s), end_s - min_clip_s), end_s
    return start_s, max(min(round(new_end, 3), end_s), start_s + min_clip_s)
