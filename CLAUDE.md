# clipforge

Semi-automated clipping pipeline for one operator (Carson). It turns **campaign-supplied** long-form footage into
vertical clips, gets human approval in Telegram, publishes via Upload-Post, and tracks Content Rewards submissions.

Read before any work: `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/COMPLIANCE.md`, `docs/TASKS.md`.
`src/clipforge/schemas.py` is the interface contract between modules. Change it only in Phase 0 or with an explicit
note in your summary, because four parallel branches depend on it.

## Non-negotiables (full list and tests in docs/COMPLIANCE.md)

- Never add code that likes, follows, comments, DMs, or otherwise automates engagement. Never set `first_comment`.
- Never publish without a stored human approval. Never auto-approve, including in tests against live APIs.
- Every caption starts with `#ad`; every render has the "Ad" label for the first 3 seconds.
- Only download sources whose URL is registered on the campaign. No scraping of Content Rewards, Vyro, or platforms.
- No live (public) publishing from tests or dev runs. `PUBLISH_MODE` defaults to `dry_run`.
- Never read, print, or log `.env` or any secret. Config comes from environment variables via `clipforge.config`.

## Niche and sources

- First niche: GTA 6 (launched 19 Nov 2026). Campaigns on Content Rewards mostly clip named streamers. First campaigns:
  Dylan Page GTA VI Marathon (`campaigns/dylan-page-gta6.json`), then AsianJeff (`campaigns/asianjeff-gta6.json`).
  Screening rules and evidence are in `docs/CAMPAIGNS.md`. Talk-format campaigns are the fallback.
- Footage priority: (1) files or links the campaign supplies (Drive, Dropbox, direct), (2) Twitch VODs/clips via yt-dlp only when
  `allow_stream_download` is true on the campaign (set from the rules, never assumed), (3) files Carson drops in `inbox/`.
  Never download from YouTube, Kick or any other platform, even if registered: refuse and alert. Footage is licensed to Carson
  only for that campaign; never reuse it elsewhere.
- Accounts start as TikTok + Instagram (`config/clipforge.yaml`, profile `gaming-1`). YouTube and X are config switches.
- Game footage: campaign `layout` (default `auto`: facecam stacked over a gameplay crop when a facecam is found, else a
  9:16 gameplay crop, else the blurred-background fit; PRD F16). Silent footage is unsupported; flag it (PRD F17),
  don't produce weak clips.
- A campaign's footage link may be a folder. Register each file separately; still download only registered URLs.
- Pay differs per platform and by viewer country (PRD F15). Never assume one CPM.

## Stack

- Python 3.12, `uv` for deps, `ruff` (lint + format), `pytest`, `mypy --strict` on `src/clipforge/schemas.py` and
  `src/clipforge/compliance.py`.
- Runs in Docker (linux/amd64) on an Intel MacBook (no GPU passthrough). Do not add macOS-native dependencies.
- ffmpeg + libass (Debian package) for cutting, cropping, captions, encoding (`libx264 -preset veryfast`).
- MediaPipe Tasks FaceDetector for reframing. PySceneDetect for shot boundaries.
- Groq `whisper-large-v3-turbo` for transcription; `faster-whisper` (CPU, int8) fallback.
- Anthropic API: `claude-haiku-5-5` (ranking), `claude-sonnet-5-5` (hooks, captions). Structured output: forced
  strict tool on Haiku; `output_config.format` JSON schema on Sonnet (Sonnet 5.5 rejects forced tool choice). No
  `temperature` (both models reject it).
- Upload-Post REST API for publishing (`httpx`). Telegram via `python-telegram-bot` v21+.
- SQLite via SQLAlchemy 2.0 + Alembic. APScheduler for jobs. Typer for the `cf` CLI.

## Layout

```
src/clipforge/
  schemas.py      # contract (pydantic v2) — Phase 0 owns
  config.py       # settings from env + config/clipforge.yaml
  db/             # SQLAlchemy models, session, alembic migrations
  compliance.py   # pure functions enforcing COMPLIANCE.md; used by publish/render/review
  costs.py        # cost ledger + monthly cap guard
  ingest/         # campaign registry, source download     (worktree A)
  transcribe/     # Groq client, chunker, local fallback    (worktree A)
  rank/           # windowing, Haiku scoring, Sonnet copy   (worktree B)
  render/         # cut, reframe, captions, overlays        (worktree C)
  qa/             # ffprobe checks                          (worktree C)
  review/         # Telegram bot                            (worktree D)
  publish/        # Upload-Post client, slot dispatcher     (worktree D)
  track/          # submissions, reconcile, YouTube views   (worktree D)
  scheduler/      # APScheduler jobs                        (worktree D)
  pipeline.py     # orchestration — Phase 2
  cli.py          # `cf` commands
prompts/          # versioned prompt templates (rank, hook, captions)
evals/            # ranking eval harness + fixtures
tests/            # mirrors src/; tests/compliance/ holds invariant tests
```

## Commands

- `make up` / `make down` / `make logs` / `make status` — Docker compose lifecycle and health.
- `make test` — `uv run pytest -q`; `make lint` — `uv run ruff check . && uv run ruff format --check .`.
- `make costs`, `make resume`, `make backup`, `make weekly-report`, `make export-reconcile`,
  `make import-reconcile FILE=...`.
- `uv run cf --help` — CLI.

## Conventions

- Every external call goes through one client class per service, with timeouts, retries (tenacity, exponential,
  max 3) and cost recording. Mock all external HTTP in tests: `respx` for httpx clients; `httpx2.MockTransport` for
  the anthropic SDK (it sends through httpx2, which respx cannot patch; see tests/rank/anthropic_mock.py).
- Pure functions for anything testable without I/O (windowing, crop paths, ASS generation, caption rules).
- Every state change of a clip/post writes an `events` row (audit log).
- Times: store UTC; schedule and display in `America/New_York`.
- Small commits with clear messages. Run `make lint test` before every commit.
- When a fact about an external API is uncertain (field names, endpoints), check the official docs URL listed in
  ARCHITECTURE.md and note what you verified in your summary. Do not guess field names.
