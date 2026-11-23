# feat/ops — Phase 1 section D: review, publish, track, scheduler

Everything Carson touches in the evening (Telegram bot, arming, publishing, submit-now, reconcile) and the worker that
runs it. Code against `schemas.py` as shipped; no contract change is needed (see "Contract requests").

## Tasks done

| Task | What | Where |
| --- | --- | --- |
| D1 | Telegram bot: owner-only gate, previews, approve / reject (reason menu) / New hook / Shorter (stubs for Phase 2), `/status /queue /skip /costs /reconcile`, alert delivery, bot heartbeat, worker-heartbeat watchdog | `review/bot.py`, `service.py`, `keyboards.py`, `previews.py`, `deliver.py`, `notify.py`, `views.py`, `alerts.py` |
| D2 | Evening flow: Arm / Skip message, arming rules, Submit-now with deadline, +20 min reminder, Submitted / Missed | `review/evening.py` |
| D3 | Slot planner (pure) and dispatcher with `dry_run` / `private` / `live`, invariants 5, 6, 7, 9, 11 | `publish/slots.py`, `dispatcher.py`, `queries.py` |
| D4 | Upload-Post payload builder (invariants 2, 4, 8, 9) and client (publish + status polling) | `publish/upload_post.py` |
| D5 | Reconcile (CSV and bot), YouTube views, weekly learning, weekly/monthly reports | `track/*.py` |
| D6 | APScheduler worker, all 12 jobs of ARCHITECTURE §4.9 | `scheduler/worker.py` |

Not touched: `pyproject.toml`, `uv.lock`, `schemas.py`, `db/`, `config.py`, `compliance.py`, other sections' folders,
`docs/TASKS.md` (boxes are ticked in Phase 2, as the rank PR did).

## Behaviour worth knowing

- **Nothing publishes without all of:** an armed evening (#6), a fresh operator approval newer than the clip's latest
  render (#5), a platform the campaign and the account allow (#9), no earlier post of that clip/file to that platform
  (#7), and a caption that starts with `#ad` (#2). `dry_run` (the default) builds and validates the real payload, then
  makes no HTTP call and returns `https://example.invalid/<platform>/<post id>`; its Submit-now message says
  "dry run: do not submit".
- **Unarmed at the first slot** marks the evening skipped, alerts, and returns scheduled clips to APPROVED. Arming is
  refused after that, or once the first slot has arrived.
- **Slots are filled lazily** (on arming and every 30 s tick) because approvals continue until ~20:10. Priority first,
  then approval order; a slot missed by more than 5 minutes is never published late; its clip rolls to the next slot.
- **Crash safety:** the post row (`request_id` = post id) is committed as PUBLISHING before the HTTP call; an unsent
  post is re-sent (not before 2 min) with the same `request_id` / `Idempotency-Key`, after re-checking the gates and the
  operator window, and failed after 10 min.
- **Approve buttons are tied to one render:** Approve / Reject carry the render stamp, so a button on an old preview
  cannot approve a re-rendered clip the operator has not watched.
- **Private mode** (PRD F9): TikTok `SELF_ONLY`, YouTube `private`, Instagram skipped (no post row is created).
- **Alerts** are `events(entity="alert")` rows (the convention branch A already uses). The bot delivers them, plus the
  spend-cap pause event, with a restart-safe cursor.
- **Reconcile:** CSV import validates every row first and applies nothing if any row is bad; `paid_usd` is a total, so
  re-importing the same file changes nothing. In the bot, Approved / Paid ask "reply with the total $ amount".
- **Learning** never treats missing view data as "0 views": no data means unlabeled.

## Tests added (319 new; full suite 548 passed, 1 skipped, 5 xfailed — the 5 belong to ingest / render / pipeline)

| Area | Tests | Covers |
| --- | --- | --- |
| `tests/publish/test_slots.py` | 17 | day types, DST dates, window property test (hypothesis), assignment order |
| `tests/publish/test_upload_post.py` | 51 | every platform's flags, private mode, `#ad` on all captions, no engagement field, respx for the request shape, retries, 4xx not retried, key never in errors/logs, status parsing |
| `tests/publish/test_dispatcher.py` | 28 | dry-run end to end, armed / unarmed, stale / non-operator / rejected-after-scheduling approval, priority and FIFO, late and missed slots, political and account-limited platforms, same-file twin, inactive campaign, private-mode HTTP path, polling, give-up, crash resume |
| `tests/review/*` | 93 | owner gate (strangers get no reply), approve / reject / stale taps incl. old-preview buttons after a re-render, hooks, keyboards <= 64 bytes, notifier HTTP shape (respx), previews (proxy, no proxy, re-render), alert cursor, watchdog, arm message, arming rules, Submit-now, reminders, Submitted / Missed |
| `tests/track/*` | 91 | CSV round trip, atomic import, idempotency, bad rows, bot reconcile conversation, YouTube (respx), labels and few-shots, reports, kill / scale |
| `tests/scheduler/test_worker.py` | 30 | job table, cron / interval fire times incl. DST and "first Sunday", error isolation and throttled alerts, secrets redacted, lazy targets, every job end to end |
| `tests/ops/test_cli_targets.py` | 8 | `cf worker/bot/report weekly/export-reconcile/import-reconcile` resolve; data commands work; a bad CSV prints a clean error |

All external HTTP is mocked with respx (Upload-Post, Telegram, YouTube). A spike confirmed respx intercepts
python-telegram-bot, so no fake `Bot` was needed. The Anthropic SDK is not used anywhere in this section, so the
`httpx2.MockTransport` rule does not apply. Nothing runs in `live` mode and nothing touches the network or a secret.

## Edits outside my folders (minimal, as asked)

**`tests/test_cli.py`** (merge hotspot with the ingest branch). Net: 3 insertions, 6 deletions.
- In `test_later_phase_commands_name_their_owner`: deleted the five `"Phase 1 D"` rows (`worker`, `bot`, `report weekly`,
  `export-reconcile`, `import-reconcile`). Left as they were, `cf bot` would have started a real bot and `cf worker` a
  blocking scheduler. Their replacement is `tests/ops/test_cli_targets.py`.
- In `test_every_target_is_known_to_be_missing_now`: the loop now skips targets whose owner contains `"Phase 1 D"`.
- Nothing else. The ingest rows (`campaign ...`, `source add`) and the `run --once` row are untouched; when both
  branches merge, expect to keep both deletions and both loop-skip conditions.

**`tests/compliance/`**: deleted only the `@xfail_until(...)` line of the 8 test functions that now pass (strict XPASS
would fail the run): `test_planned_slots_inside_window` (inv11), the four `build_payload` tests
(`test_disclosure_flags_per_platform` inv04, `test_upload_payload_caption_starts_with_ad` inv02,
`test_upload_payload_has_no_engagement_fields` inv08, `test_upload_payload_x_caption_has_no_urls` inv09; three are
parametrized over platforms), `test_dispatcher_skips_stale_approval` (inv05),
`test_dispatcher_unarmed_evening` (inv06) and `test_bot_ignores_other_chats` (live-mode file). Where the `xfail_until` import became unused it was removed (ruff).
Nothing else in those files changed.

## External facts verified (2026-11-22)

**Upload-Post**
- https://docs.upload-post.com/api/upload-video: `POST https://api.upload-post.com/api/upload`; header
  `Authorization: Apikey <key>`; optional `Idempotency-Key` (we send the post id); fields `user`, `platform[]`, `video`,
  `title`, `request_id`, `external_id`, `async_upload`; per-platform `tiktok_title` / `instagram_title` / `x_title` /
  `youtube_title`; `description` is ignored for TikTok and Instagram; TikTok `privacy_level`, `post_mode`,
  `disable_inbox_fallback`, `brand_content_toggle`, `brand_organic_toggle`, `is_aigc`; YouTube `youtube_description`,
  `categoryId`, `privacyStatus`, `selfDeclaredMadeForKids`, `containsSyntheticMedia`, `hasPaidProductPlacement`;
  Instagram `media_type`, `share_to_feed`, `is_ai_generated`; X `made_with_ai`, `x_paid_partnership`. The API has
  auto-comment fields; we never send any.
- https://docs.upload-post.com/api/upload-status: `GET https://api.upload-post.com/api/uploadposts/status?request_id=`,
  statuses `pending queued processing in_progress completed failed not_found`, `results` is an array of
  `{platform, success, message, upload_timestamp, ...}`.
- https://docs.upload-post.com/guides/paid-partnership: `paid_partnership` with TikTok `SELF_ONLY` is refused
  (400 `commercial_disclosure_privacy_conflict`); the explicit `brand_content_toggle` wins over it; Instagram has no
  native label on this API (the `#ad` caption is the disclosure).
- **Not verified:** the URL field name inside a status result (only TikTok's `post_url` is mentioned; we accept
  `post_url` or `url`); the async accept body (`{"success": true, "request_id": ...}` is from a summary); the 2.2 MB
  OpenAPI spec (https://docs.upload-post.com/openapi.json) could not be read in full.

**Telegram** (https://core.telegram.org/bots/api, Bot API 10.5): method URL format and `{"ok", "result"}` envelope;
`sendMessage` text 1-4096; `sendVideo` caption 0-1024 and bots can send videos up to 50 MB; `answerCallbackQuery`;
`getUpdates` long polling. **Not verified from the page:** the 64-byte `callback_data` limit (enforced anyway),
sending from two processes with one token (the worker sends, only the bot polls), and 409 behaviour.

**YouTube** (https://developers.google.com/youtube/v3/docs/videos/list, `.../determine_quota_cost`):
`GET https://www.googleapis.com/youtube/v3/videos`, `part=statistics`, comma-separated `id`, 1 quota unit per call,
public data needs only an API key. **Not verified:** the name `key` for the key parameter, a maximum of ids per call
(we batch at 50), the wire type of `viewCount` (we accept a string or a number), whether missing ids are omitted.

## Decisions to review

1. The worker sends Telegram messages itself; the bot handles the button callbacks.
2. A deadline passing without a tap does not auto-mark MISSED; it stays AWAITING and shows in `/reconcile`.
3. "Paid views" in the monthly kill/scale report are estimated as `paid_usd / CPM x 1000` (no API for the real number).
   Day 0 is the first real (non-dry-run) post; day >= 60 and < 100k paid views -> CHANGE, day >= 120 -> STOP.
4. YouTube titles are prefixed with `#ad ` (invariant 2 says every caption); X posts also set `x_paid_partnership`.
5. A private-mode TikTok post may be rejected by TikTok itself because branded content cannot be private. If so the
   private-mode run in Phase 3 needs Carson's decision; invariant 4 is not weakened here.
6. `arm_message` runs every minute (one message per evening) instead of once at 20:00, so a failed send is retried.

## Contract requests (`docs/contract-requests/ops.md`)

No `schemas.py` / `db/` change is required. Seven items: (1) SQLite WAL, (2) preview-proxy path from render,
(3) `captions_json` shape from rank/pipeline, (4) re-render must refresh `rendered_at` so approvals go stale,
(5) alert and spend-cap pause delivery conventions, (6) `arm_message` cadence note for ARCHITECTURE §4.9,
(7) Upload-Post facts that differ from ARCHITECTURE §4.7.

## Left for Phase 2 / 3

- Wire "New hook" / "Shorter": pass `service.ReviewHooks` to `bot.run_bot` (the cf command calls it without hooks).
- `pipeline.run_once` and `ingest.purge.purge_sources` are called by the worker when they exist.
- Phase 2 re-render semantics for approvals (contract request 4); a clip already SCHEDULED must be released first.
- Tick D1-D6 in `docs/TASKS.md`.
- Phase 3: first real `private` run (TikTok branded-content / `SELF_ONLY` risk above), confirm the status URL field,
  decide on WAL.

## Compliance review

The `compliance-reviewer` subagent reviewed `git diff origin/main...HEAD` (55 files): **PASS, no FAIL items**. It found no
invariant weakened (#2, #4, #5, #6, #7, #8, #9, #11, live-mode guard, owner-chat rule preserved; #1, #3, #10, #12
untouched by this branch), `tests/compliance/` edits limited to deleting `@xfail_until` lines and then-unused imports,
no real network or live mode in tests, no scraping, no secrets in logs or Telegram messages. It made five non-blocking
recommendations:

| # | Recommendation | Outcome |
| --- | --- | --- |
| 1 | An old preview's Approve could approve a newer render once New hook / Shorter are wired (callback carried only the clip id) | **Fixed.** Approve / Reject buttons now carry a render stamp (`rendered_at` in whole seconds); `approve_clip` / `reject_clip` require it and refuse a mismatch. Tests: service and bot (old Approve and old reject-reason are refused after a re-render; the new preview's Approve works). |
| 2 | The poller could re-send an unsent post while the dispatcher's upload is still running | **Fixed.** Unsent posts are re-sent only after `RESEND_AFTER` (2 min). `request_id` / `Idempotency-Key` remain the second line of defence. |
| 3 | A late re-send could fall outside the operator window | **Fixed.** The re-send path also runs `assert_slot_in_window` on `now` and fails the post (with an alert) outside the window. |
| 4 | A clip refused by `assert_unique_post` stays SCHEDULED and raises the alert again each evening | Not changed (nothing is ever published; it is alert noise only). Left for Phase 2 to decide whether to expire such clips. |
| 5 | Unverified Upload-Post facts (TikTok branded + `SELF_ONLY`, status URL key, async body) | Already flagged above and in contract request 7; needs a private-mode smoke test before the first live evening. Not live mode. |
