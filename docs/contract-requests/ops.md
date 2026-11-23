# Contract requests from `feat/ops` (Phase 1 D)

`schemas.py` needs no change. Nothing below blocks this branch; each item is either a convention another branch must
meet, or a change to a file Phase 1 branches may not edit (db/, Dockerfile, ARCHITECTURE.md).

## 1. SQLite: use WAL (db/session.py) — recommended

The worker and the bot are two processes writing one SQLite file, and the worker holds a transaction open while it
talks to Telegram or Upload-Post in a few places (`review.evening.send_*`, `publish.dispatcher` polling). In the default
rollback-journal mode a reader blocks the other process's commit until it finishes. Proposed in `make_engine`'s connect
hook next to the existing pragmas: `PRAGMA journal_mode=WAL` (and `synchronous=NORMAL`). Without it the 15 s busy
timeout is the safety net; with Docker bind mounts on macOS, check WAL works on the mounted volume before adopting it
(ARCHITECTURE §1 already keeps the DB inside the container, so it should).

## 2. Render (C6): preview-proxy path for clips over 50 MB

Telegram bots can send videos up to 50 MB. `review.previews.pick_video` sends `clip.path` when it fits, otherwise
`<clip stem>.preview.mp4` next to it (e.g. `clip_12.mp4` -> `clip_12.preview.mp4`), otherwise a text-only notice with no
buttons and an operator alert (a clip that cannot be watched cannot be approved from Telegram). TASKS C6 says "480p
preview proxy for Telegram when > 50 MB" without a path; please use this naming or tell me the one you chose.

## 3. Rank / pipeline: shape of `candidates.captions_json`

`publish.queries.caption_for` reads `{"<platform>": "<text>"}` (also accepts `{"<platform>": {"text": ...}}` and a list of
`{"platform": ..., "text": ...}`), falling back to `candidates.hook`. Whatever is stored, the text is re-normalized by
`compliance.normalize_caption` when the payload is built, so a missing `#ad` can never reach a platform. The pipeline
should store `copy_.captions` in the first form.

## 4. Phase 2: re-render must make approvals stale (invariant 5)

"New hook" / "Shorter" (the bot calls `service.ReviewHooks.new_hook/shorter`, stubs today) must (a) set
`clip.rendered_at` to the new render time, (b) move the clip `RENDERED -> PENDING_REVIEW` again, and (c) leave the old
approval row alone. `CLIP_TRANSITIONS` allows `PENDING_REVIEW -> RENDERED` and `APPROVED -> RENDERED`, but not
`SCHEDULED -> RENDERED`: a clip already in a slot has to be released to APPROVED first (the dispatcher's
`_release` does this when an evening is skipped or a slot is missed; for a re-render of a SCHEDULED clip Phase 2
should call `publish.dispatcher.skip_evening`-style release or add that step). The dispatcher and the arm queue compare
every approval with `clip.rendered_at` (`compliance.assert_human_approved`), and the bot sends a fresh preview whenever
`rendered_at` differs from the one last previewed (`flags.preview_sent:<clip_id>`).

## 5. Phase 2: wire alerts and the spend-cap pause

- Any module can raise an operator alert with `review.alerts.raise_alert(session, kind, entity_id, message)`; it writes
  `events(entity="alert")`, the same convention the ingest branch uses. The bot delivers them (cursor in
  `flags.alerts_cursor`) and also delivers the `events(entity="pipeline", to_state="paused")` row that `costs.pause`
  writes, so the pipeline needs no Telegram code for the spend-cap alert.
- `scheduler.worker` calls `pipeline.run_once(settings)` and `ingest.purge.purge_sources(session, settings)` lazily and
  skips them with a log line while they do not exist.

## 6. ARCHITECTURE.md §4.9: `arm_message` cadence

The table says "20:00 Mon-Fri; weekend times from config". The job runs every 60 s instead; the function sends one
message per evening between `first_slot - arm_message_minutes_before_first_slot` and the first slot, so a failed send is
retried. Net behaviour is the same on weekdays and follows the config on weekends.

## 7. Docs: Upload-Post facts that differ from or add to ARCHITECTURE §4.7

Verified 2026-11-22 (details and URLs in `publish/upload_post.py` and the PR description):

- TikTok and Instagram captions travel in `tiktok_title` / `instagram_title` (the `description` field is ignored for
  them); YouTube uses `youtube_title` (we prefix `#ad `) plus `youtube_description`; X uses `x_title`.
- Upload-Post accepts an `Idempotency-Key` header; we send it with `request_id` (both are our post id).
- **Private mode TikTok risk:** the docs say `paid_partnership=true` with `privacy_level=SELF_ONLY` is refused with HTTP
  400 `commercial_disclosure_privacy_conflict`. We send `brand_content_toggle=true` (invariant 4) and not
  `paid_partnership`, but the docs do not say whether TikTok itself accepts branded content with `SELF_ONLY`. If the
  Phase 3 private-mode run (PRD §7.4) fails with that error, the options are a private test without the branded flag on
  a throwaway account, or a one-clip public run Carson approves; invariant 4 must not be weakened without his approval.
- The status response schema is only partly documented (results array with `platform/success/message`; the URL field
  name is not shown). The parser accepts `post_url` or `url` and keeps polling until one is present.
