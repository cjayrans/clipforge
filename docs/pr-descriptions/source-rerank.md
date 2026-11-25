## `cf source rerank <id>`

Runs only the ranking stage again on an already ranked source, reusing its download and transcript (no new download or transcription cost).

- Allowed only for a source in status `ranked` on an **active** campaign that has a transcript; anything else is refused.
- Goes RANKED -> TRANSCRIBED through `set_source_status` (events row, actor `operator`). The next nightly run ranks it again.
- Existing candidates and clips are history and are never altered or re-rendered. Candidates that were never rendered (still `candidate`) are expired through `transition()` (events row), so only the new candidates are rendered.
- New candidates carry the current weights version (already stored by the moment ranker).
- The ranker never proposes a window overlapping footage of a clip that is approved, scheduled or published (pre-filter in `rank_moments`, plus a post-filter in the pipeline for both rankers).
- Nothing here publishes anything; no compliance rule is touched.

Tests: `tests/ingest/test_rerank.py`, CLI end to end, ranker overlap exclusion, and a nightly-run test showing old candidates and approved clips stay unchanged.
