# Phase 1 B: `feat/rank` (windows, prompts, Haiku scorer, Sonnet copy, eval harness)

Implements docs/TASKS.md section B (B1–B5). Edits only `src/clipforge/rank/`, `prompts/`, `evals/`, `tests/rank/`,
`tests/evals/`, `docs/contract-requests/rank.md` and this file. No shared files touched: not `schemas.py`,
`config.py`, `db/`, `compliance.py`, `pyproject.toml`/`uv.lock`, `cli.py`, `tests/compliance/`, `tests/test_cli.py`
or `docs/TASKS.md`.

## Tasks done

| Task | What | Main files |
| --- | --- | --- |
| B1 | Window builder (pure): sentence units from word punctuation (falls back to pauses and segment ends), windows 20–75 s on unit boundaries, stride 10 s, lengths near 30/45/60 s; pre-score from speech density, question→answer and optional per-second energy; top 60 | `rank/windows.py` |
| B2 | `prompts/score_windows.md`, `prompts/clip_copy.md` with `name`/`version` front matter; loader; campaign brief; few-shot injection from the `fewshots` table (5 positive + 5 negative, newest week first); transcript text escaped and fenced as data; version tag `score_windows@1+clip_copy@1` stored on each candidate | `rank/prompts.py`, `prompts/` |
| B3 | `ClaudeClient` (the one Anthropic client): `costs.guard()` before each call, `costs.record()` after (input + output tokens, priced by the requested model), **each in its own committed transaction from a `ledger` session factory, so a caller's rollback can't erase spend**, tenacity retries (3 attempts, exponential) on 408/409/429/5xx/connection errors, `RankError` on refusal, `max_tokens`, missing tool call or bad JSON. Haiku scorer: batches of 10, forced strict tool `score_windows`, each item validated against `schemas.WindowScore` (invalid, unknown and duplicate ids dropped and counted); a refused batch is skipped and reported. Pure candidate selection: drops `brief_ok=False`, −15 for non-standalone, trims snapped to word boundaries, overlap de-dup, top N | `rank/claude_client.py`, `rank/score.py`, `rank/select.py` |
| B4 | Sonnet copywriter → `schemas.ClipCopy` (structured outputs, effort `low`); hook repaired to ≤ 8 words / 60 chars and title to ≤ 60 chars; captions only for `compliance.allowed_platforms(campaign, account platforms)`, each through `compliance.normalize_caption`. `rank_source` chains windows → score → select → copy and returns candidates (persists only cost rows) | `rank/copy.py` |
| B5 | `uv run python -m evals.run`: replay mode by default (offline, through the same request and validation code via a mock transport), `--live` opt-in. Reports schema validity, precision/recall@k and bad@k against `evals/labels.csv` (IoU ≥ 0.5), and top-k stability across `--repeat N` live runs. JSON report in `<data dir>/evals/` | `evals/run.py`, `evals/labels.csv`, `evals/fixtures/replay/` |

`evals/labels.csv` ships with four **seed placeholder** labels that I wrote from the fixture text. Carson should
replace them with real labels over time.

Replay output on the fixtures: `transcript_talk` validity 1.00, P@5 0.50, R@5 0.50, bad@5 0;
`transcript_sparse` validity 1.00, bad@5 1. The sparse fixture is below the no-speech guard; transcribe (A4b) stops it
before ranking in the real pipeline.

## Tests added (332 passed, 1 skipped, 22 xfailed for the full suite; `rank/` coverage 99%)

- `tests/rank/test_windows.py`: hypothesis property tests (lengths inside bounds, starts and ends on unit
  boundaries inside the transcript, unique ids, ≤ keep, sorted, stride respected, deterministic); fixtures; short,
  empty, unpunctuated and segment-only transcripts; long-sentence split; question→answer and energy effects.
- `tests/rank/test_prompts.py`: header and section parsing, bad files, template variables, `$` safety, tag escaping,
  campaign brief, few-shot rendering and DB loading order.
- `tests/rank/test_claude_client.py`: exact request body (model, strict forced tool, `output_config`, no
  `temperature`/`top_p`/`top_k`/`thinking`), auth and API-version headers, cost rows and USD, retries on
  429/500/529/408 then success, give-up after 3, no retry on 400, spend cap blocks **before** any HTTP request,
  refusal/max_tokens/missing tool/bad JSON/empty text, missing key, cost rows survive a caller rollback.
- `tests/rank/test_score.py`, `tests/rank/test_select.py`: strict-compatible schema, batching (25 → 10/10/5),
  validation and filtering, missing ids, refused batch skipped, selection ranking, trims, overlap, top N.
- `tests/rank/test_copy.py`: Sonnet request uses structured outputs and no `tool_choice`; every caption starts with
  `#ad `; required tags present; no URLs on X; political campaign gets no TikTok/X captions; account ∩ campaign
  platforms only; no double disclosure; hook/title repair; length cut; `rank_source` end to end on the talk fixture;
  a candidate whose copy is refused is dropped and reported.
- `tests/evals/test_run.py`: replay run end to end, determinism, malformed replay items counted invalid, `--live`
  without a key sends nothing, `--live` without a database refuses, stability, metric helpers, and a run stopped by
  the cap keeps its spend while the next run sends nothing (invariant 12 regression).

How Anthropic is mocked: anthropic 2.12.0 sends requests through **httpx2**, which respx can't intercept. With
Carson's OK, tests inject `httpx2.Client(transport=httpx2.MockTransport(...))` via `ClaudeClient(http_client=...)`
(`tests/rank/anthropic_mock.py`), so every test still checks the real HTTP request body. Tests use only the fake key
`sk-test-not-a-real-key`. No network.

## External facts verified (2026-11-22)

| Fact | Source |
| --- | --- |
| Model IDs `claude-haiku-5-5`, `claude-sonnet-5-5`; prices $0.10/$0.50 (Haiku, prompts ≤ 100K tokens; $0.50/$2.50 above) and $2/$10 per MTok (Sonnet), which match `config.prices` | https://platform.claude.com/docs/en/models/overview, https://platform.claude.com/docs/en/models/haiku-5-5/overview |
| Both models run adaptive thinking by default; `temperature`, `top_p`, `top_k` return a 400 | https://platform.claude.com/docs/en/models/sonnet-5-5/overview, https://platform.claude.com/docs/en/models/haiku-5-5/overview |
| Claude Sonnet 5.5 rejects forced `tool_choice` (`any`/`tool`) with a 400; use `auto` + strict tools or structured outputs | https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide#forced-tool-use |
| Structured outputs: `output_config.format = {type: "json_schema", schema}`; strict tools `strict: true`; no `minimum`/`maximum`/`minLength`/`maxLength`/`minItems > 1`; `additionalProperties: false` required | https://platform.claude.com/docs/en/build-with-claude/structured-outputs |
| SDK in the lockfile: anthropic 2.12.0, sends `Authorization: Bearer` and `anthropic-version: 2026-10-21`, uses httpx2; `messages.create` has no `temperature` parameter | checked in `.venv` and asserted in `tests/rank/test_claude_client.py` |

## Deviations from the docs (please confirm)

1. **Sonnet copy uses structured outputs, not forced tool use**, because forced tool use is a 400 on Sonnet 5.5.
   Haiku scoring keeps the forced `score_windows` tool. See contract request 2.
2. **No temperature 0.** The eval measures top-k stability across repeated runs instead. See contract request 3.
3. **Anthropic tests mock with httpx2.MockTransport, not respx.** See contract request 4.
4. No server-side refusal fallback and no prompt caching, so the cost ledger stays exact (only configured models
   are priced). A refusal skips that batch or candidate and is reported.

## Contract requests (`docs/contract-requests/rank.md`)

1. Dockerfile: `COPY prompts ./prompts` (**required**: the image doesn't contain prompts today).
2. CLAUDE.md/ARCHITECTURE: forced tool use isn't available on Sonnet 5.5.
3. ARCHITECTURE: replace "determinism at temperature 0".
4. CLAUDE.md conventions: httpx2.MockTransport for the anthropic SDK.
5. Optional per-second energy input (`Transcript.energy_db_per_s` or a transcribe helper).
6. Optional `rank:` config section for the module constants.
7. Cost rows commit on their own: Phase 2 passes a session factory and must not hold an uncommitted SQLite write
   while ranking.
8. Informational: Haiku's long-prompt price tier.

## Compliance review

The `compliance-reviewer` subagent reviewed `git diff main...HEAD` twice.

- **First pass: FAIL on invariant 12.** Cost rows were only flushed into the caller's transaction. An early stop
  (spend cap, API error, Ctrl-C) rolled them back, so the spend disappeared from the ledger and the next run could
  spend again. The reviewer reproduced it: 15 paid requests, 0 rows left.
- **Fix (d006358):** `ClaudeClient` takes `ledger: sessionmaker[Session]` instead of a session (**caller-facing
  change**). The guard check and each pair of cost rows run in their own committed transaction. Two regression
  tests were added.
- **Second pass: PASS.** Invariants 2, 8, 9 and 12 are preserved; the others are untouched. The reviewer's probe:
  `run1_requests=15 run2_new_requests=0 cost_rows=30 mtd=15.00 cap=15.0`.
  `uv run pytest -q tests/compliance tests/rank tests/evals` gave 233 passed, 22 xfailed (all pre-existing), and
  `make lint` is clean.

## Left for Phase 2

- `pipeline.py`: call `rank_source(...)` per transcribed source with
  `ClaudeClient.from_settings(settings, make_session_factory(engine))` and `load_fewshots(session)` (commit pipeline
  writes first: see contract request 7); persist each `Candidate` as a `CandidateRow` (`hook`, `title`, `captions_json` from
  `copy_`, `prompt_version`), write `events`, move the source to `RANKED`; catch `SpendCapReached` → `costs.pause`.
- Bot "New hook" → `rank.copy.write_copy(candidate, clip_text, campaign, platforms, client=...)`, then re-render
  the overlays.
- Wire per-second energy into `build_windows` if contract request 5 is accepted.
- Tick B1–B5 in `docs/TASKS.md` (not edited here to avoid a merge hotspot).
- No `tests/compliance/` xfail waits on `rank`, so no markers were removed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
