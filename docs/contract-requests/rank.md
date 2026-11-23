# Contract requests from `feat/rank` (Phase 1 B)

`schemas.py` needs no change. The requests below touch files Phase 1 branches may not edit (Dockerfile, config,
CLAUDE.md, ARCHITECTURE.md), or record facts that contradict them.

## 1. Dockerfile: copy `prompts/` into the image (required)

`rank` loads `prompts/score_windows.md` and `prompts/clip_copy.md` at run time. The image installs the package
non-editable and copies only `src`, `alembic.ini` and `scripts`, so in Docker `load_prompt` would raise
`PromptError: prompt file not found`. Proposed (next to `COPY scripts ./scripts`):

```dockerfile
COPY prompts ./prompts
```

`clipforge.rank.prompts.prompts_dir()` uses `<cwd>/prompts` when it exists (`/app/prompts` in the image, since
`WORKDIR /app`), else the repo checkout's `prompts/`. `evals/` is a developer tool and does not need to be in the image.

## 2. Docs: Claude Sonnet 5.5 can't use forced tool use (CLAUDE.md "Stack", ARCHITECTURE §4.3)

CLAUDE.md says "Structured output via forced tool use with a JSON schema". Checked on 2026-11-22: Claude Sonnet 5.5
rejects `tool_choice` `any`/`tool` with a 400
(https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide#forced-tool-use). What this branch does:

- Haiku scoring (`claude-haiku-5-5`): forced tool `score_windows` with `strict: true`, as ARCHITECTURE says.
  Haiku 5.5 accepts forced tool use; the call skips thinking.
- Sonnet copy (`claude-sonnet-5-5`): structured outputs (`output_config.format`, `type: json_schema`), effort `low`.

Proposed wording for CLAUDE.md: "Structured output: forced strict tool on Haiku; `output_config.format` JSON
schema on Sonnet (Sonnet 5.5 rejects forced tool choice)."

## 3. Docs: no temperature 0 (ARCHITECTURE §4.3 "determinism at temperature 0")

Claude Haiku 5.5 and Claude Sonnet 5.5 reject `temperature`/`top_p`/`top_k` (400), and the pinned SDK
(anthropic 2.12.0, API version `2026-10-21`) doesn't expose them. The eval measures *stability* instead: top-k
Jaccard across `--repeat N` live runs. Proposed: replace "determinism at temperature 0" with "top-k stability across
repeated runs".

## 4. Docs/convention: respx can't mock the Anthropic SDK (CLAUDE.md "Conventions")

anthropic 2.x sends requests through `httpx2`, and respx only patches `httpx`. Anthropic tests inject
`httpx2.Client(transport=httpx2.MockTransport(...))` through `ClaudeClient(http_client=...)`; see
`tests/rank/anthropic_mock.py`. Other services (Groq 1.7 and our own httpx clients) still use `httpx`, so respx is
still the right tool for them. Proposed: "Mock all external HTTP in tests: respx for httpx clients;
`httpx2.MockTransport` for the anthropic SDK."

## 5. Pipeline input: per-second audio energy (optional, Phase 2)

`rank.windows.build_windows(..., energy_per_s=...)` takes a per-second loudness list (any monotonic unit, e.g. dB
from ffmpeg `astats`/`ebur128`). Nothing produces it yet. Options:

- add `energy_db_per_s: list[float] | None = None` to `schemas.Transcript` (filled by transcribe, which already has
  the audio), or
- a separate helper in transcribe that Phase 2 calls and passes to `rank_source(energy_per_s=...)`.

Without it, the pre-score uses speech density and question→answer only. Ranking still works.

## 6. Optional `rank:` config section

These are module constants today; config would let them be tuned without a code change:

| Constant | Where | Value |
| --- | --- | --- |
| window stride | `rank.windows.build_windows(stride_s=)` | 10 s |
| target window lengths | `rank.windows.TARGET_LENGTHS_S` | 30, 45, 60 s |
| score batch size | `rank.score.BATCH_SIZE` | 10 |
| copy effort | `rank.copy.COPY_EFFORT` | `low` |
| non-standalone penalty | `rank.select.NOT_STANDALONE_PENALTY` | 15 points |
| overlap de-dup threshold | `rank.select.MAX_OVERLAP` | 0.5 |

## 7. Cost rows commit on their own (Phase 2: pass a session factory)

The compliance review found that cost rows were only flushed into the caller's transaction, so a rollback after
an early stop (spend cap, API error, Ctrl-C) erased spend that really happened, and the next run could spend again
(invariant 12). Fixed: `ClaudeClient(..., ledger=sessionmaker)` runs each `costs.guard()` check and each pair of
`costs.record()` rows in its own short transaction and commits at once. For Phase 2:

- build the client with `ClaudeClient.from_settings(settings, make_session_factory(engine))`;
- don't call `rank_source` / `write_copy` while holding an uncommitted write on the same SQLite file (the ledger
  commit would wait for that lock, then fail after the 15 s busy timeout). Commit pipeline writes before ranking
  and persist candidates afterwards;
- `costs.record` itself still only flushes. Other paid clients (Groq in transcribe) may want the same pattern.

## 8. Cost ledger: Haiku long-prompt tier (informational)

Claude Haiku 5.5 costs $0.50/$2.50 per MTok for prompts over 100K tokens (https://platform.claude.com/docs/en/models/haiku-5-5/overview);
`config.prices` has only the ≤100K rate. Scoring prompts are about 3–5K tokens (10 windows), so the flat price is
exact for this use. Something to know if a future caller sends very long prompts.
