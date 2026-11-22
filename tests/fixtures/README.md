# Test fixtures

| File | What | How it was made |
| --- | --- | --- |
| `synthetic_90s.mp4` | 90 s, 1920×1080, 30 fps, H.264 + AAC 440 Hz sine. Three shots with hard cuts at 30 s and 60 s (testsrc, testsrc2, smptehdbars). No faces. ~5 MB. | `make fixtures` (runs `scripts/make_fixture_video.py` inside the Docker image) |
| `transcript_talk.json` | `schemas.Transcript`, 90 s of dense game commentary (~140 wpm), 14 segments, word timestamps. | Segment text written by hand in `scripts/make_fixture_transcripts.py`; word timings spread evenly per segment |
| `transcript_sparse.json` | `schemas.Transcript`, 90 s with ~11 wpm: below the 20 wpm no-speech guard (PRD F17). | Same script |

`tests/fixtures/local/` is git-ignored: put real campaign footage there for Phase 3 benchmarks. Never commit campaign
footage (it is licensed to the operator for that campaign only).
