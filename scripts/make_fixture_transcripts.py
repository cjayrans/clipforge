"""Write tests/fixtures/transcript_*.json from hand-written segment text.

The segment text below was written by hand for tests (original, fictional commentary). Word timings are spread
evenly across each segment, so the files are deterministic.
Re-run after editing: `uv run python scripts/make_fixture_transcripts.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

from clipforge.schemas import Segment, Transcript, Word

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# (start_s, end_s, text): dense game commentary, about 150 words per minute over 90 s.
TALK = [
    (0.0, 5.8, "Okay chat, we are finally inside the bank and nobody has noticed us yet."),
    (6.2, 12.0, "Wait, wait, why is the alarm already going off? I did not touch anything."),
    (12.4, 18.6, "Do you think the guard saw the car? Because I parked it on the sidewalk like an idiot."),
    (19.0, 24.8, "Yeah, he definitely saw the car. Okay, new plan, we go out through the roof."),
    (25.2, 31.4, "Is there even a ladder up here? No? Great. Amazing. Love this for us."),
    (31.8, 38.0, "Hold on, there is a helicopter on the next building and I think the door is open."),
    (38.4, 44.6, "How far can you jump in this game? Let's find out, live, with everybody watching."),
    (45.0, 50.8, "I made it! I actually made it! Clip that, somebody clip that right now."),
    (51.2, 57.4, "Okay, I have never flown one of these before, so what is the worst that could happen?"),
    (57.8, 63.6, "The worst that could happen is apparently flying straight into a billboard."),
    (64.0, 70.2, "Why does the billboard have my face on it? Who made this? Was it you guys?"),
    (70.6, 76.4, "Somebody in chat paid for a billboard of me just to make me crash. That is commitment."),
    (76.8, 82.6, "Alright, we are alive, the money is gone, and the police are very, very close."),
    (83.0, 89.6, "Honestly, best heist we have ever done. Same time tomorrow? Same time tomorrow."),
]

# Mostly music/gameplay with a few words: about 13 words per minute, under the 20 wpm no-speech threshold.
SPARSE = [
    (4.0, 6.0, "Okay, here we go."),
    (31.0, 33.5, "Oh no. Oh no no."),
    (58.0, 60.0, "That was close."),
    (85.0, 88.0, "Alright, see you tomorrow."),
]


def build(source_id: int, segments: list[tuple[float, float, str]], duration_s: float) -> Transcript:
    words: list[Word] = []
    for i, (start, end, text) in enumerate(segments):
        tokens = text.split()
        step = (end - start) / len(tokens)
        for j, tok in enumerate(tokens):
            w_start = round(start + j * step, 2)
            w_end = round(start + (j + 1) * step - 0.04, 2)
            prob = round(0.80 + ((i * 7 + j * 3) % 20) / 100, 2)
            words.append(Word(text=tok, start_s=w_start, end_s=w_end, prob=prob))
    return Transcript(
        source_id=source_id,
        provider="groq",
        model="whisper-large-v3-turbo",
        language="en",
        words=words,
        segments=[Segment(text=t, start_s=s, end_s=e) for s, e, t in segments],
        duration_s=duration_s,
    )


def main() -> None:
    for name, sid, segs in (("transcript_talk.json", 1, TALK), ("transcript_sparse.json", 2, SPARSE)):
        t = build(sid, segs, 90.0)
        (OUT / name).write_text(json.dumps(t.model_dump(mode="json"), indent=1) + "\n", encoding="utf-8")
        print(f"{name}: {len(t.words)} words, {len(t.words) / (t.duration_s / 60):.0f} wpm")


if __name__ == "__main__":
    main()
