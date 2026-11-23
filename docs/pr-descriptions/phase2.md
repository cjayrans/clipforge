# Phase 2: integration (`feat/phase2` → `main`)

This branch turns the four Phase 1 modules into one working system:
- the nightly pipeline (`pipeline.run_once`);
- the bot's New hook and Shorter buttons;
- every contract request resolved;
- a fully mocked end-to-end test of PRD §7.2;
- the last compliance xfail removed;
- a Docker image that contains everything the pipeline loads at run time.

**`schemas.py` is unchanged.** No contract request needed a schema change.

## Tasks done (docs/TASKS.md, all boxes in sections A–D and P2 ticked)

| Task | What | Main files |
| --- | --- | --- |
| P2.1 | Every contract request resolved (table below) | `config.py`, `db/session.py`, `compliance.py`, `cli.py`, `Dockerfile`, callers |
| P2.2 | Nightly orchestration: per-stage error isolation, early stop at the candidate target, spend-cap pause | `pipeline.py` |
| P2.3 | New hook / Shorter wired to rank + render, with fresh-approval semantics | `pipeline.py` (`begin_rerender`, `finish_rerender`, `make_review_hooks`), `rank/select.py` (`shorter_range`), `review/bot.py` |
| P2.4 | End-to-end test (PRD §7.2), fully mocked | `tests/pipeline/test_e2e.py` |
| P2.5 | No `xfail` left in `tests/compliance/`; all pass | `tests/compliance/test_inv12_spend_cap.py` |
| P2.6 | Make targets end to end; `make up` runs both services healthy | see "Verification" |

## How the nightly run works (`pipeline.run_once`)

1. `costs.guard()`. At the cap the run pauses (`costs.pause`) and stops. An existing pause is not recorded again, so
   you get one alert, not one per night.
2. `scan_inbox`.
3. Sweeps:
   - expire unpublished clips of drained or ended campaigns;
   - recover clips that a crashed re-render left in RENDERED for over an hour.
4. Render the candidates left from earlier nights. This costs no API money.
5. For each source of an active campaign (priority, then oldest; at most `max_sources_per_night`):
   - download;
   - guard, then transcribe;
   - guard, then rank;
   - render the new candidates.
6. Stop once `nightly_candidate_target` clips passed QA **in this run** (your choice in planning). Candidates not
   rendered stay CANDIDATE for the next night.

Rules:
- **Short transactions.** Every step uses its own short transaction. No write is held open during a paid call, a
  download or a render, because the bot writes to the same SQLite file.
- **Cost rows commit on their own.** Claude cost rows already did. Groq now does too (new optional
  `GroqTranscriber(ledger=)`), so a rollback can never erase real spend (invariant 12).
- **Stage failures.**
  - An expected failure is recorded by its module (status + alert) and the run moves on to the next source.
  - An unexpected one is rolled back and becomes a `pipeline_stage_failed` alert.
  - `SpendCapReached` anywhere pauses the run.
- **One account set per clip.** Clips are made for the first account set whose platforms the campaign allows. That
  gives one clip per candidate, so the same content never goes to two accounts.
- **Render folders.** Each render goes into `media/clips/<slug>/<account set>/r<stamp>/`.
  - A clip over 50 MB also gets `<clip>.preview.mp4` (ops CR 2).
  - A failed render is retried once, the next night. After that the candidate expires, with an alert.
- **Empty runs.** A run that produces no clips sends one alert saying why.

## New hook / Shorter (P2.3)

- `cf bot` now builds `pipeline.make_review_hooks`. The button is answered at once and the work runs in a background
  thread.
  - Telegram's docs say clients show a progress bar until the bot answers. They give no time limit
    (https://core.telegram.org/bots/api#callbackquery, checked 2026-11-23).
  - A render takes minutes, so the bot must not wait for it.
- `begin_rerender`:
  - refuses finished clips;
  - refuses a second tap while one is running;
  - releases a SCHEDULED clip that has no posts from its slot;
  - moves the clip to **RENDERED before any paid call**, so neither the dispatcher nor an old Approve button can act
    on it meanwhile.
- `finish_rerender`:
  - New hook gets new copy from Sonnet. Shorter uses `rank.select.shorter_range`: it cuts 20% from the end whose cut
    part holds fewer words, snaps to word/sentence boundaries and keeps ≥ `min_clip_s`.
  - Then it renders into a new folder and runs QA.
  - On success it swaps the clip to the new file, sets `rendered_at` **later than every earlier decision**
    (`fresh_render_time`), and moves the clip RENDERED → PENDING_REVIEW. It writes events, updates the candidate's
    hook/captions or range, and deletes the superseded folder (never posted).
- Old approval rows are kept. They are now stale (invariant 5). The bot sends a new preview because `rendered_at`
  changed, and the old preview's Approve button is refused by its render stamp.
- **Any failure, QA included,** keeps the old file. The clip goes back to review with a new `rendered_at`, so it
  needs a fresh approval too, and you get a `rerender_failed` alert.

## Contract requests: resolution

| Request | Resolution |
| --- | --- |
| Ingest CR-A1: alert delivery | **Closed.** Ops' `review.deliver.deliver_pending_alerts` sends every `events(entity="alert")` row with a cursor in `flags.alerts_cursor` (option b). `ingest.alerts.alert_operator` now delegates to `review.alerts.raise_alert`, so there is one implementation. Test: `test_ingest_alerts_reach_the_operator`. |
| CR-A2: `transcribe:` config | Optional `TranscribeConfig` block (`local_fallback`, `local_model`, `language`) with defaults, so older YAML stays valid. `transcribe.local`, `GroqTranscriber` and `LocalTranscriber` read it. |
| CR-A3: exit codes | `cf campaign check` exits 1 when a check fails. |
| CR-A4: docstring | `ingest/__init__.py` signatures corrected. |
| CR-A5: pipeline wiring | Done in `pipeline.py`. Expiry on drain/end: new `publish.dispatcher.expire_campaign_clips`, called by `cf campaign drain/end` and the nightly sweep. Added `cf source retry <id>` (FAILED → REGISTERED, events row). |
| CR-A6: refused-host helper | `compliance.is_refused_platform_url`. `ingest.download.is_refused_platform_host` delegates to it. Test in `tests/compliance/test_inv01_sources.py`. |
| Rank 1: Dockerfile prompts | `COPY prompts ./prompts`. The build fails if the prompts don't load. |
| Rank 2–4: wording | CLAUDE.md (Sonnet structured outputs, no temperature, `httpx2.MockTransport`) and ARCHITECTURE §4.3. |
| Rank 5: energy input | **Deferred.** Optional; ranking works on speech density and question→answer. |
| Rank 6: `rank:` config | **Deferred.** The constants are fine for v1; tune after the Phase 3 benchmark. |
| Rank 7: ledger | The pipeline passes its session factory and never holds a write while ranking. |
| Rank 8 | Informational (Haiku long-prompt price tier). |
| Render 1: Dockerfile assets | `COPY assets ./assets`. The build fails if a font or the face model is missing. |
| Render 2: `CLIPFORGE_ASSETS_DIR` | **Declined.** Discovery works in the repo, in CI and in `/app`. |
| Render 2b: blank label | Config strips `render.ad_label_text` and rejects blank text. Exactly "Ad" is not required. Render keeps its own check. |
| Render 3: inputs | The pipeline passes every `ClipRequest` field. |
| Render notes | Public `compliance.has_tag` (QA uses it instead of a copied regex). QA checks the YouTube title (`#ad ` + title ≤ 100) when YouTube is a target. |
| Ops 1: WAL | `journal_mode=WAL` and `synchronous=NORMAL` for file databases. Backups are written as self-contained single files. Checked on the Docker bind mount (see "Verification"). |
| Ops 2: proxy path | The pipeline builds `<clip>.preview.mp4`, the name `review.previews` reads. |
| Ops 3: captions shape | `candidates.captions_json` is stored as `{platform: text}`. |
| Ops 4: re-render semantics | See New hook / Shorter above. Public `dispatcher.release_clip` for a SCHEDULED clip. |
| Ops 5: alerts and pause | Delivered by the bot; the pipeline has no Telegram code. |
| Ops 6–7: docs | ARCHITECTURE §4.9 (`arm_message` cadence) and §4.7 (Upload-Post facts). |
| Ops reviewer note 4 | A clip refused by invariant 7 on every platform now expires once with an alert, instead of re-queueing every evening. |

## Open question for you (needs a decision before the Phase 3 private run)

**TikTok may reject branded content (`brand_content_toggle=true`) when privacy is `SELF_ONLY`.**
- Upload-Post documents that `paid_partnership=true` with `SELF_ONLY` is refused (400
  `commercial_disclosure_privacy_conflict`).
- It does not say what TikTok does with the branded-content toggle that we send.
- If the private run (PRD §7.4) fails this way, the options are:
  - a private test without the branded flag, on a throwaway account; or
  - a one-clip public run that you approve.
- **Invariant 4 is unchanged here.** Changing it needs your explicit approval in a PR. The same note is in
  ARCHITECTURE §4.7.

## Edits to `tests/compliance/`

- **`test_inv12_spend_cap.py::test_pipeline_pauses_at_cap`**
  - Removed the xfail marker.
  - The test could never pass as written: its `session` fixture used `tmp_path/test.db`, while
    `run_once(settings)` opens `settings.database_url`. It now opens that database.
  - It now also asserts that a second paused run records no second pause event (no duplicate alert).
  - The invariant is unchanged; the check is stricter.
- **`test_inv05_human_approval.py`:** new `test_rerender_makes_earlier_approval_stale`. Approve → New hook → the old
  approval fails `assert_human_approved` and the armed dispatcher publishes nothing.
  - It first failed because the test clock put the re-render before the approval. That led to `fresh_render_time`,
    so `rendered_at` is always after every recorded decision.
- **`test_inv01_sources.py`:** new `test_is_refused_platform_url`.
- No marker or assertion was weakened. `xfail_until` stays in `_helpers.py` unused, for future phases.

## Other shared test edits

- **`tests/test_cli.py`:** the "later phase / not implemented" tests became `test_every_target_resolves` and
  `test_run_once_on_empty_db_prints_a_summary`. Every command is implemented now.
- **`tests/review/test_bot.py`:** the default-stub text changed.
- **`tests/render/test_clip.py`:** the blank-label case is now refused by config. Render's own refusal is still
  tested, with an unvalidated config.
- **Pipeline fixtures** live in `tests/support/pipeline_fixtures.py` and `pipeline_fakes.py`, registered as a pytest
  plugin in `tests/conftest.py`.

## Tests added

| File | Covers |
| --- | --- |
| `tests/pipeline/test_e2e.py` | The full flow below |
| `tests/pipeline/test_run_once.py` | See below |
| `tests/pipeline/test_rerender.py` | See below |
| `tests/rank/test_shorter.py` | `shorter_range` cases, including hypothesis properties |
| `tests/publish/test_expiry.py` | Drain/end expiry, idempotence, `release_clip`, the invariant 7 dead end |
| Other | CLI exit code and `cf source retry`, config (blank label, transcribe block), Groq ledger and language, WAL and backup, QA YouTube title, ingest alert delivery |

**`test_e2e.py`, full flow:**
- one registered source;
- download, mocked with respx;
- Groq, mocked with respx using the talk fixture;
- Haiku and Sonnet, mocked with httpx2.MockTransport;
- real `render_candidate` and `qa_clip` with injected media fakes;
- previews sent to a fake notifier;
- Approve through `bot.on_callback`;
- the unarmed slot publishes nothing;
- arm;
- dry_run publish to TikTok and Instagram: the Upload-Post route is never called and URLs are `example.invalid`;
- Submit now;
- Submitted through the bot;
- `/reconcile`: Paid $12.50 and Rejected.

It asserts the events trail, captions (`#ad `, required tags, `{platform: text}`) and cost rows for Groq, Haiku and
Sonnet. `test_e2e_with_real_render` repeats the flow with real ffmpeg, ffprobe, PySceneDetect and MediaPipe. It
skips on the Mac and runs in CI and Docker.

**`test_run_once.py`:**
- an empty run (one alert, no spend);
- early stop, then backlog first the next night (no API calls);
- a failing source doesn't block the next one;
- the spend cap mid-run keeps the Groq cost row, makes no Anthropic call, and pauses;
- render retry, then expiry;
- a QA failure never reaches review;
- sweeps.

**`test_rerender.py`:**
- New hook makes the approval stale;
- the dispatcher refuses the old approval;
- a SCHEDULED clip is released;
- Shorter cuts ≥ 20%;
- a failure keeps the old file;
- finished clips are refused;
- a double tap is refused;
- the hook runner answers at once and finishes in the background.

## External facts verified

- **Telegram Bot API** (https://core.telegram.org/bots/api, 2026-11-23):
  - After a callback button is pressed, clients show a progress bar until `answerCallbackQuery` is called. The page
    gives **no** time limit for answering.
  - `callback_data` is "1-64 bytes". The ops PR listed this as unverified.
- Nothing else external changed. Groq, Anthropic and Upload-Post field names are as verified in the Phase 1 PRs; no
  new endpoint or field is used.

## Verification

Filled in from the actual runs below.
