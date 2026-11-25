# PRD — clipforge v1

## 1. Problem

Carson clips paid campaigns (Content Rewards first, Vyro later) with about 7 hours a week: Monday–Friday 8–9pm ET and
one hour each weekend day. Content Rewards requires every post to be submitted within **30 minutes of publishing**,
pays ~$1 per 1,000 verified views, and settles rounds ~10 days after they open. Hand editing at this rate pays below
minimum wage, so production must be automated while judgement (approval) and submission stay human.

## 2. Goals (v1)

1. Produce about **8 candidate clips per night** from campaign-supplied footage, ready by 8pm ET.
2. Let Carson approve ~3 clips in under 10 minutes from his phone.
3. Publish each approved clip to the platforms its campaign allows (TikTok and Instagram Reels first; YouTube Shorts
   and X optional) at fixed evening slots,
   **only on evenings he has armed**, and alert him with each live URL and its submission deadline.
4. Record submissions, brand decisions, views and earnings; feed outcomes back into ranking weekly.
5. Stay within **$15/month** of API spend (LLM + transcription), enforced by a cap.
6. Enforce every rule in `docs/COMPLIANCE.md` in code.

## 3. Non-goals (v1)

- Automating Content Rewards or Vyro submission or reading their dashboards (no API; terms don't permit scraping).
- Any engagement automation (likes, follows, comments, DMs).
- Visual highlight detection for silent gameplay (v1 ranks from the transcript, so it needs narrated or commentary
  footage), multi-speaker active-speaker tracking, music, AI voices or faces.
- Multi-user support, a web UI, cloud hosting.

## 4. User flows

### 4.1 Sunday: add a campaign (operator, ~30 min)
1. Carson pastes a campaign brief into Claude chat (prompt in `CLAUDE_CHAT_PROMPTS.md`) and gets a JSON registry
   entry plus a pass/fail filter check.
2. `cf campaign add --from-json campaign.json` validates against `schemas.CampaignIn` and stores it.
3. `cf source add <campaign_id> <url>` registers each footage link from the brief. Only registered URLs can be
   downloaded.

### 4.2 Nightly (automatic, 01:00 ET)
For each active, non-drained campaign with unprocessed sources: download → transcribe → rank → render the top
candidates → QA → send previews to Telegram. Stop early once `nightly_candidate_target` clips are ready.

### 4.3 Evening (operator, 8:00–9:00pm ET)
1. 8:00 — bot posts the **Arm** message: tonight's slots and the clips queued for them, with buttons
   **Arm tonight** / **Skip tonight**. Nothing publishes until armed. If not armed by the first slot, tonight's slots
   are skipped and the clips roll to the next evening.
2. 8:00–8:10 — Carson approves/rejects tomorrow's candidates (buttons: Approve, Reject + reason, New hook, Shorter).
3. Each slot (default 20:10, 20:25, 20:40) publishes one clip to every enabled platform for that campaign.
4. As each URL becomes available the bot sends **Submit now**: campaign name, the campaign's submission link, the post
   URL, the deadline (published_at + 30 min) and buttons **Submitted** / **Missed**. A reminder fires at +20 min if
   unmarked. If the platform has published the post but not released its link (TikTok can take hours), the same
   message goes out without the link, says so, and tells Carson to copy the link from the app and send
   `/posturl <post id> <link>`; the deadline still counts from the publish time.

### 4.4 Saturday: reconcile (operator, ~30 min)
`/reconcile` in the bot walks through open submissions: status buttons (Approved / Rejected / Pending / Paid) and an
optional earnings reply. CSV alternative: `make export-reconcile` → edit → `make import-reconcile`.

### 4.5 Weekly/monthly reports (automatic)
Saturday 08:00: digest (posts, submission rate, approvals, paid views, earnings, costs, top/bottom clips). First Sunday
of the month: monthly report incl. kill/scale rule status (day-60: <100k paid views/month → change; day-120 → stop).

## 5. Functional requirements

| ID | Requirement |
| --- | --- |
| F1 | Campaign registry with rate, min payout, per-clip cap, budget remaining, allowed platforms, required tags, disclosure text, rules notes, political flag, status (active/drained/ended). |
| F2 | Source download only for URLs registered on a campaign; Google Drive, Dropbox and direct links supported; Twitch VOD/clip links only for campaigns with `allow_stream_download`; YouTube and other hosts refused; manual `inbox/` drop folder; SHA-256 recorded; sources deleted 7 days after their clips are rendered. |
| F3 | Transcription with word timestamps; audio extracted as mono 16 kHz MP3 ~32 kbps and chunked under 24 MB with 2 s overlap; local fallback if Groq fails twice. |
| F4 | Candidate windows 20–75 s on sentence boundaries; Haiku scores up to 60 windows per source; Sonnet writes hook (≤ 8 words), YouTube title (≤ 60 chars) and per-platform captions. |
| F5 | Render 1080×1920, 30 fps, H.264 + AAC, loudness −14 LUFS, ≤ 90 s; talk footage: static crop per shot from face detection, blurred-background fit when no face or faces too far apart; game footage: the campaign `layout` (F16); word-highlight captions; hook overlay first 2.5 s; "Ad" label first 3 s; one style preset per account. |
| F6 | QA gate (ffprobe + manifest): duration 10–90 s, resolution, audio present, disclosure present, caption rules, campaign active and platform allowed. Failures never reach review. |
| F7 | Telegram bot restricted to `TELEGRAM_OWNER_CHAT_ID`; approval, arming, submission marking, reconcile; commands `/status`, `/queue`, `/skip`, `/reconcile`, `/costs`. |
| F8 | Publisher calls Upload-Post at slot time with the per-platform flags in ARCHITECTURE.md §6; polls for post URLs; idempotent via `request_id` = post id. |
| F9 | `PUBLISH_MODE`: `dry_run` (no API call, fake URLs), `private` (TikTok `SELF_ONLY`, YouTube `private`, Instagram skipped), `live`. Default `dry_run`. |
| F10 | Cost ledger for every Anthropic/Groq call; pipeline pauses at `monthly_api_cap_usd`; `cf resume` clears the pause. |
| F11 | YouTube view snapshots daily via Data API `videos.list` (statistics) when `YOUTUBE_API_KEY` is set. |
| F12 | Weekly learning job: label clips by outcome, store top-5/bottom-5 examples with hooks as few-shots for the ranking prompt; prompts are versioned and the version is stored on each candidate. |
| F13 | Nightly SQLite backup (`.backup`), keep 14. |
| F14 | Health: heartbeat file every minute; bot alert if the worker heartbeat is older than 10 min during 19:30–21:00. |
| F15 | Per-platform economics: store `cpm_by_platform`, `min_payout_by_platform`, `unpaid_viewer_countries`, `participants` and `budget_total_usd` on a campaign; the QA gate rejects a post to a platform the campaign doesn't allow. |
| F16 | Game footage (`content_kind == "game"`), campaign `layout` (default `auto`): **stacked** (the streamer's facecam, detected once per source or set as `facecam_box`, on top 1080×640; a gameplay crop below 1080×1280) when a face is in the facecam for the clip; else **crop** (full-height 9:16 gameplay crop); else **fit** (blurred-background fit). The gameplay crop is AutoFlip-style: stationary centre by default, stationary off-centre when the action holds still there, tracking (max 15 % of the width per second, smoothed) only when concentrated motion leaves the centre; a crop-layout shot whose action splits across the frame gets the fit. Each clip records its layout, why others were skipped and the render time; the review preview shows the layout. |
| F18 | Campaign-specific behaviour is data, not code: a campaign JSON may add an overlay image shown on every clip (placed clear of the "Ad" label, captions, hook and platform UI), required caption text appended at the end of every caption, a copy style note for the hook/caption prompt, the Twitch channels its VODs may come from, and a cap on how many minutes of a stream are downloaded. Adding a campaign is one JSON file plus its assets in `campaigns/assets/<slug>/`. |
| F19 | "New hook" never shows a hook the clip already had: every previous hook is sent to the copy model as an avoid-list, a repeat is retried once, and a second repeat keeps the current version. |
| F17 | Sources may be a folder link (Google Drive or Dropbox): list the files, register each as a source of that campaign, hash each. No-speech guard: if a transcript has fewer than 20 words per minute, mark the source FAILED with reason `no_speech` and tell the operator instead of ranking it. |

## 6. Non-functional

- Runs unattended on `docker compose` with `restart: unless-stopped`; survives reboots.
- All secrets from env; nothing secret in logs (redaction filter).
- Unit-test coverage ≥ 80% on `rank/`, `render/` pure functions, `compliance.py`, `publish/` payload building.
- A full nightly run for 2 one-hour sources finishes in under 3 hours on the Mac (benchmark in Phase 3).

## 7. Acceptance criteria for v1

1. `make test lint` green in CI (GitHub Actions, Ubuntu).
2. End-to-end test with mocked Groq/Anthropic/Upload-Post/Telegram: one source → candidates → render → QA → approve →
   arm → publish (dry_run) → submit-now message → submitted → reconcile.
3. Every invariant in COMPLIANCE.md has a passing test in `tests/compliance/`.
4. Private-mode run on one real campaign source: clips appear privately on TikTok (Instagram is skipped in private mode), URLs arrive in
   Telegram, crop and captions look right to Carson.
