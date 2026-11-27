# Architecture — clipforge v1

## 1. Overview

One Docker image, two long-running services on the operator's Intel MacBook:

- `worker`: APScheduler process that runs the nightly pipeline, the evening slot dispatcher, URL polling, reports,
  backups, and view snapshots.
- `bot`: Telegram long-polling process for approvals, arming, submission marking and reconciliation.

Both share `./data` (SQLite DB, media) and `./config` (YAML). They communicate only through the database (no IPC).

```
Sunday (operator)            Nightly 01:00 (worker)                      Evening 20:00–21:00
-----------------            --------------------------------           ---------------------------------
cf campaign add  --->  DB --> ingest -> transcribe -> rank -> render  --> bot: previews -> approve
cf source add                    |          |          |        |         bot: Arm tonight
                                 v          v          v        v         worker: slot dispatch -> Upload-Post
                              sources   transcripts candidates clips      worker: poll URLs -> bot: Submit now
                                                                         operator: submits on Content Rewards
Saturday: /reconcile -> submissions/earnings -> weekly learning job -> few-shot examples -> rank prompt
```

## 2. Clip lifecycle (state machine, `schemas.ClipState`)

```
CANDIDATE -> RENDERED -> QA_FAILED (terminal)
                      -> PENDING_REVIEW -> REJECTED (terminal)
                                        -> APPROVED -> SCHEDULED -> PUBLISHED -> (per-post states)
                                                    -> EXPIRED (campaign drained/ended before publish)
```

Per-platform `Post` states (`schemas.PostState`): `QUEUED -> PUBLISHING -> LIVE | FAILED`, then submission state
(`schemas.SubmissionState`): `AWAITING -> SUBMITTED | MISSED`, then brand outcome
(`schemas.BrandDecision`): `PENDING -> APPROVED | REJECTED`, and `paid_usd` accumulates.

Allowed transitions live in `schemas.CLIP_TRANSITIONS`; `db` refuses any other transition and every transition writes
an `events` row.

Post status interpretation (`publish.upload_post.interpret_status`, shapes observed live 2026-11-24): Upload-Post's
per-platform `status` decides, never `success` alone. `queued`/`pending`/`processing`/`in_progress`/`retryable` keep
polling (an unfinished job reports `success: false`); only `failed` (or `success: false` on a finished entry) fails the
post. A finished entry with `success: true` and no `post_url` is **published, link not released yet** (TikTok releases the
public post ID after moderation, up to hours later): the post stays `PUBLISHING` with `published_at` set, the submission
is opened from that publish time, the Submit-now notice goes out without a URL and tells the operator to copy the link
from the app and send `/posturl <post id> <link>`, and the poller keeps checking (every tick for 30 min, then every
5 min, then hourly) and alerts only if the link has not come after 24 h. `FAILED -> PUBLISHING` exists for one reason:
`cf post repair` (operator, dry run by default) moves a post that was wrongly marked failed back, after Upload-Post
reports it completed. It never re-sends (the post keeps its `upload_post_job`; invariant 7).

## 3. Data model (SQLite)

| Table | Key columns |
| --- | --- |
| `campaigns` | id, platform_marketplace (`content_rewards`/`vyro`), name, url, submission_url, cpm_usd, min_payout_usd, per_clip_cap_usd, budget_remaining_usd, allowed_platforms (json), required_tags (json), disclosure_text, rules_notes, is_political, approval_rate_pct, overlay (json), copy_style_notes, stream_channels (json), max_source_minutes, moment_profile, status, created_at |
| `sources` | id, campaign_id, url, sha256, local_path, duration_s, status, downloaded_at, purged_at |
| `transcripts` | id, source_id, provider, model, words_json_path, language, cost_usd |
| `candidates` | id, source_id, start_s, end_s, score, standalone, brief_ok, reasons, hook, title, captions_json, prompt_version, state; moment ranker: moment_profile, weights_version, archetype, score_reaction/event/setup/absurdity, gate_results_json, skip_flags, review_flags |
| `clips` | id, candidate_id, account_set, style_preset, path, duration_s, sha256, qa_report_json, state |
| `approvals` | id, clip_id, decision, reason, decided_at, telegram_message_id |
| `evenings` | date, armed_at, skipped, slots_json |
| `posts` | id, clip_id, platform, slot_at, request_id, upload_post_job, post_url, published_at, state, error, views_24h, views_72h |
| `submissions` | id, post_id, campaign_id, deadline_at, state, submitted_at, brand_decision, decided_at, paid_usd |
| `view_snapshots` | id, post_id, taken_at, views, source (`youtube_api`/`manual`) |
| `fewshots` | id, kind (`positive`/`negative`), candidate_id, hook, transcript_excerpt, outcome_note, week |
| `costs` | id, at, service, model, units, unit_kind, usd |
| `events` | id, at, entity, entity_id, from_state, to_state, actor (`system`/`operator`), detail |

## 4. Modules

### 4.1 ingest
- `registry.py`: add/list/update campaigns from `schemas.CampaignIn` JSON; `drain`/`end` commands. Before saving,
  `validate_campaign_setup` checks a campaign's overlay (file, RGBA, placement clear of captions/hook/platform UI, a
  spot for the "Ad" label) and refuses URL-like required caption text on a campaign that lists X (invariant 9).
- Optional, generic campaign fields (all campaign-specific behaviour lives in the campaign JSON, never in code):
  `overlay` {path under `campaigns/assets/`, position {x_pct, y_pct}, scale, full_duration}; `required_tags` (the
  JSON may call it `required_caption_tags`: text every caption ends with, hashtags or a link); `copy_style_notes`
  (tone for the copy prompt); `stream_channels` (Twitch logins whose VODs/clips may be downloaded);
  `max_source_minutes` (download only the first N minutes of a stream source); `moment_profile` (name of a file in
  `config/moment_profiles/`: switches ranking to the moment ranker, §4.3).
- `download.py`: only `sources` rows. Handlers by host: Google Drive (`gdown`), Dropbox (`?dl=1`), direct HTTP; Twitch VOD/clip
  (`yt-dlp`) only when the source's campaign has `allow_stream_download=true`; with `stream_channels` the metadata is
  read first and a video from another channel (or a VOD still live) is refused before any media is fetched; with
  `max_source_minutes` only `[0, N min]` is downloaded (yt-dlp `download_ranges`). Any other host, including YouTube and Kick, is
  refused with an operator alert. Refuses any URL not registered (`compliance.assert_registered_source`). Stores SHA-256 and
  duration (ffprobe).
- `inbox.py`: watches `inbox/<campaign_slug>/` for files Carson drops in by hand (footage the campaign supplied as a download he
  made himself); registers each as a source with `url = file://inbox/...` after confirming the campaign is active.
- Twitch VODs expire (up to 60 days) and Twitch clips are ≤ 60 s: register the source early, download once, keep the hash.
- Purge job deletes source media 7 days after all its candidates are terminal. moments-v2: the nightly run also deletes
  a RANKED source's file as soon as nothing is pending (`ingest/purge.purge_if_done`); unrendered candidates expire 3
  days after ranking. Before a download, `ingest/disk.py` checks free space (Twitch: the VOD's size from yt-dlp
  metadata) plus `min_free_gb`.
- VOD proposals (moments-v2, `ingest/twitch_helix.py`): a nightly job (00:30) looks up recent archive VODs of each
  campaign's `stream_channels` with the official Twitch Helix API (app token; only campaigns with
  `auto_propose_vods`, `allow_stream_download` and `recent_vod_days`) and writes at most 2 `vod_proposals` per campaign.
  The bot sends each with Add / Dismiss; only Add registers the URL (`registry.add_source`). Nothing is registered
  or downloaded automatically.

### 4.2 transcribe
- Extract audio: `ffmpeg -i in -vn -ac 1 -ar 16000 -b:a 32k out.mp3` (~14 MB per hour).
- Chunk to ~30 min (always < 24 MB) with a 45 s overlap. One `silencedetect` pass moves cuts into silences and puts
  the stitch seam in a silence inside each overlap; words are merged onto one global timeline (each word once, then
  duplicates removed by text + timestamp). Signals, the content pass, candidate windows and clip cuts all use this
  global timeline and the full source file, never a chunk (tests in `tests/rank/test_boundaries.py`).
- Groq: `client.audio.transcriptions.create(model="whisper-large-v3-turbo", response_format="verbose_json",
  timestamp_granularities=["word","segment"])`. Docs: https://console.groq.com/docs/speech-to-text
  (the docs page has a conflicting code comment about `json` vs `verbose_json` — verify word timestamps in a test).
- Fallback: `faster-whisper` `large-v3-turbo`, `device="cpu"`, `compute_type="int8"` — slow; only on two Groq failures.
- Output: `schemas.Transcript` (words with start/end/prob, segments).

### 4.3 rank
- `windows.py` (pure): build windows 20–75 s aligned to segment boundaries, stride ~10 s; pre-score by speech density,
  question→answer patterns, and audio energy peaks (ffmpeg `astats`/`ebur128` per second, computed in transcribe or
  render utils). Keep top 60.
- `score.py`: Haiku, batches of 10 windows, forced strict tool `score_windows` with JSON schema from
  `schemas.WindowScore`. Prompt inputs: campaign brief summary + rules, few-shots (`fewshots` table), windows text.
- `copy.py`: Sonnet for the top N (`rank.top_n_per_source`, default 8) via structured outputs
  (`output_config.format` JSON schema; Sonnet 5.5 rejects forced tool choice): `schemas.ClipCopy` = hook (≤ 8 words),
  youtube_title (≤ 60 chars), captions per platform. `compliance.normalize_caption` then forces `#ad` first, appends
  required campaign tags, strips URLs for X, enforces platform length limits.
- Prompts live in `prompts/*.md` with a `version:` header; the version is stored on each candidate.
- Moment ranker v2 (campaigns with `moment_profile`; research in `docs/research/clipping-gta6-moment-types.md`). All
  niche knowledge is in the profile YAML (`config/moment_profiles/<name>.yaml`, `rank/profile.py`): the moment
  catalogue (archetypes with examples and signs: examples of what good looks like, never a list of allowed moments),
  the reject list, the six-part rubric (hook, arc, arousal, sendability, clarity, density) with weights and
  `weights_version`, window rules, content-pass and vision settings, signal thresholds, word lists, skip rules and
  review flags.
  - `content_pass.py`: the whole transcript in 12 min segments with 2 min overlap, Haiku forced tool
    `propose_moments` (start, end, trigger, payoff, archetype or `other`, confidence, reason); quiet and deadpan
    moments included; proposals deduplicated on the global timeline (a whole proposal beats one cut at a segment
    edge).
  - `media_signals.py` / `signals.py` / `moments.py`: cheap loudness/motion/transcript signals over the whole source
    propose more candidates (trigger + reaction pairs, strong reactions alone). Signals are evidence only: nothing is
    dropped for being quiet, and the old trigger+reaction requirement is gone.
  - `candidates.py`: content and signal candidates merged by overlap, `found_by` content / signals / both.
  - `moment_score.py`: Haiku forced tool `score_moments`, 8 candidates per call (by candidate, never by time slice),
    each with 30 s of transcript before and after; six rubric scores, `has_trigger` / `has_payoff`, window, skip /
    review flags, reason. Gates `brief`, `skip_rules` (publisher policy), `content` (clear trigger and payoff),
    `window` (a moment that cannot fit 45 s is dropped, never cut), `min_score`. Window: open on the most
    interesting beat a stranger can follow, no dead air, end right after the payoff, 15-30 s target, 45 s cap; a
    payoff past the window extends it.
  - `vision.py`: keyframes (one per 2.5 s, max 8, plus facecam crops) of the best 12 go to Haiku as image blocks
    (forced tool `judge_frames`): visible event / payoff / reaction, scene, spoiler and graphic risk; corrected rubric
    scores replace the transcript ones; findings map to profile skip rules / review flags. Cost per candidate is
    recorded.
  - Copy (Sonnet) is written only for the candidates the nightly run picks to render, across all sources.
  - Review flags (sexual/graphic) never reject: the Telegram preview shows "CHECK BEFORE APPROVING", plus the rubric
    scores, `found_by` and the frames verdict.
  - Calibration: candidates store the rubric scores, `found_by`, vision result, archetype and weights version; posts
    take watch time, completion, shares/sends and likes (`cf post metrics`); `cf report moments` shows rubric
    correlations from 20 posts. `cf source shadow-rank` ranks a source without writing candidates and reports.
- `evals/`: harness that runs scoring on fixture transcripts and checks schema validity, top-k stability across
  repeated runs (Haiku/Sonnet 5.5 reject `temperature`, so there is no temperature 0), and ranking agreement with
  Carson's labels (`evals/labels.csv`, filled over time).
- `ClaudeClient` takes a session factory (`ledger=`): each spend-cap check and cost row commits in its own short
  transaction, so a caller's rollback can't erase real spend. Callers must not hold an uncommitted SQLite write while
  calling it.

### 4.4 render
- `shots.py`: PySceneDetect `ContentDetector` on the candidate range → shot list.
- `reframe.py` (pure core): for each shot sample 5 fps, MediaPipe FaceDetector → face boxes; choose the largest face
  (v1); crop x-center = median of its centers; crop width = src_h × 9/16. If no face, or the two largest faces' centers
  are further apart than the crop width → `fit_blur` mode (scaled source over blurred, zoomed copy). Output
  `schemas.CropPlan` (per shot: mode, x, w, h). Talk footage only (game footage uses the layouts below).
- `layout.py` (pure): the layouts of game footage and the order they are tried. Campaign `layout: auto` →
  **stacked** (facecam box scaled into a 1080×640 top panel, gameplay crop at 1080:1280 below, `vstack`) when a
  facecam is known, then **crop** (full-height 9:16 gameplay crop), then **fit** (`fit_blur`). A layout is skipped
  when its prerequisites fail (stacked: a face in the facecam box in ≥ 60 % of the clip's sampled frames; crop: the
  source is wider than 9:16) or when `campaign_overlay.place_texts` cannot place hook, label, captions and overlay
  clear of each other, the platform UI and the face; the reasons go to `manifest.layout_reason`.
- `facecam.py` (detector v2): once per source, 40 frames sampled **inside the source's clip windows** (its
  candidates; the whole source only when it has none). Each frame is classified: **full-screen camera** when a face is
  ≥ 25 % of the frame height (whole frame and frame-height tiles), else **gameplay**; only gameplay frames count (≥ 8
  needed). Small faces come from overlapping tiles (BlazeFace short-range sees 128×128, so a small facecam face needs
  tiling), NMS; pure core `pick_facecam` clusters face centres over gameplay frames (in-game faces move, the facecam
  does not). The cluster must be in ≥ 60 % of gameplay frames and its face region must **change over time**
  (`face_variation` ≥ 6 grey levels: a picture or poster on the wall does not), then median face box, padded to the
  panel aspect (2.4 × face height, face 45 % down). Stored on `sources.facecam_json` with the counts and the reason
  ("none" too; v1 results are ignored and re-detected). A campaign `facecam_box` (percent of the frame) skips
  detection. Per clip, `face_presence` decides from the clip's own frames: a varying face in the facecam box in
  ≥ 60 % of crops and at most half the clip's whole frames full-screen camera, else no stacked.
  `cf source facecam <id> [--redetect] [--no-store] --debug-image` draws what was found.
- `saliency.py` + `crop_path.py` (pure): AutoFlip-style gameplay camera. One ffmpeg pass per clip (4 fps, 192 px
  grey) → per-sample column profile of the hottest frame-difference pixels (facecam masked). Per shot: stationary
  centre by default; stationary at the median when concentrated motion holds still off-centre; tracking only when
  concentrated motion leaves the centre window in ≥ 30 % of the shot and moves (median filter, dead zone, rate limit
  at 15 % of the source width per second, moving average, clamped, simplified to keyframes; the encoder turns them
  into a piecewise-linear `crop` x expression in `t`). Global camera motion is never "concentrated", so it never moves
  the crop. In the crop layout a shot whose motion splits into two far-apart spots gets the fit.
- `captions.py` (pure): word chunks of 2–4 words, ≤ 18 chars per line; ASS file with per-word highlight (`\k` or
  per-word events); safe zone: captions centered at 62–70% height (clear of platform UI at the bottom ~20%).
- `overlays.py`: hook text (top third) 0–2.5 s; "Ad" label 0–3 s at the left edge under the hook's largest box (default
  (48, 548); out of the platform UI zones) on a 75 %-opaque dark box; style preset (font, size, colors, outline)
  from `config.styles[account_set]`. Fonts are OFL-licensed and bundled in `assets/fonts/`.
- `campaign_overlay.py` (pure core): a campaign's overlay image (RGBA PNG). Scales it to `scale` × 1080 px, centres it
  on `position`, finds its visible box (alpha > 16) and refuses any placement whose visible box meets the caption
  band, the hook box, the frame edge or the platform UI zones (top 150 px, right 15 %, bottom 20 %). The "Ad" label
  goes to the first left-edge spot clear of it, the hook, the captions and the UI zones (default under the hook, else
  under the overlay) or the render is refused. Nothing is
  ever shrunk or moved to make it fit.
- `encode.py`: one ffmpeg filtergraph per shot then concat, or a single graph with `trim`+`crop`+`concat`;
  `-c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p -r 30 -c:a aac -b:a 128k`, `loudnorm=I=-14:TP=-1.5:LRA=11`.
  Writes `schemas.RenderManifest` (includes `has_ad_label`, `ad_label_end_s`, `style_preset`, `ad_label_rect` and the
  campaign `overlay` with its rects and timing). A campaign overlay is input 1 (a single `.png` frame), scaled and
  composited with `overlay=...:eof_action=repeat` after the shots are joined and before `ass`, so the label, hook and
  captions are drawn on top of it.

### 4.5 qa
`checks.py`: ffprobe + manifest → `schemas.QAReport` (pass/fail per check). Checks listed in PRD F6 plus
`compliance.assert_render_manifest`.

### 4.6 review (Telegram bot)
- `python-telegram-bot` v21 Application, long polling; every handler first checks `chat_id == TELEGRAM_OWNER_CHAT_ID`.
- Preview: `send_video` (≤ 50 MB; else a 480p proxy rendered for preview) with caption: campaign, score, hook,
  platforms, duration. Inline keyboard: Approve / Reject (reason menu: off-brief, boring, bad crop, bad captions,
  other) / New hook / Shorter.
- New hook → re-run `rank.copy.write_copy` for that clip, then a full re-render (overlays are burned in).
  Shorter → `rank.select.shorter_range` trims 20% from the weaker end (fewer words lost) on word/sentence
  boundaries, then a full re-render. Both run in the background (`pipeline.make_review_hooks`, wired by `cf bot`);
  the button is answered at once. The clip moves to RENDERED before any paid call (a SCHEDULED clip is released
  from its slot first), the new file goes into a new `r<stamp>` folder, and on success `rendered_at` is set later
  than every earlier decision and the clip returns to PENDING_REVIEW: earlier approvals are stale (invariant 5) and
  the bot sends a new preview. Any failure (including QA) keeps the old file and returns it to review with a new
  `rendered_at`, so it needs a fresh tap too.
- Evening: 20:00 Arm message (`evenings` row); Submit-now messages with Submitted/Missed; +20 min reminder.
- `/reconcile`: iterate open submissions (oldest first), buttons Approved/Rejected/Pending/Paid, then
  "reply with $ amount or 0".

### 4.7 publish
- `slots.py` (pure): from `config.slots` and today's date (America/New_York) → slot datetimes; assign approved clips
  FIFO by campaign priority; one clip per slot; all enabled platforms of that clip's campaign at that slot.
- `upload_post.py`: `httpx` client. **Verify every field name against** https://docs.upload-post.com/api/upload-video
  and the OpenAPI spec before coding. Endpoint `POST /api/upload` (multipart). Fields we use:
  - common: `user` (= Upload-Post profile for the account set), `platform[]`, `video`, `title`, `request_id`
    (= our post id, for idempotency), `external_id` (= our post id), `async_upload=true`.
  - TikTok: `privacy_level` (`PUBLIC_TO_EVERYONE` in live, `SELF_ONLY` in private), `brand_content_toggle=true` in
    live / `false` in private (COMPLIANCE.md invariant 4 exception), `brand_organic_toggle=false`, `is_aigc=false`,
    `post_mode=DIRECT_POST`, `disable_inbox_fallback=true`. Never `paid_partnership`, `disclose_commercial`,
    `branded_content` or `your_brand`.
  - YouTube: `privacyStatus` (`public`/`private`), `selfDeclaredMadeForKids=false`, `containsSyntheticMedia=false`,
    `hasPaidProductPlacement=true`, `categoryId` from config.
  - Instagram: `media_type=REELS`, `share_to_feed=true`, `is_ai_generated=false`. No `collaborators` (Content
    Rewards bans collab posts unless the brief allows).
  - X: `made_with_ai=false`; caption must contain no URLs (Upload-Post strips them anyway).
  - Never send `first_comment`.
  Auth header and status/URL endpoint: take from the docs (do not guess). TikTok post URLs can take ~3 min to appear.
  Verified 2026-11-22 (Phase 1 D, `publish/upload_post.py`): header `Authorization: Apikey <key>` plus
  `Idempotency-Key` (= post id); TikTok and Instagram captions travel in `tiktok_title` / `instagram_title`
  (`description` is ignored for them); YouTube uses `youtube_title` (we prefix `#ad `) and `youtube_description`; X
  uses `x_title` and also sets `x_paid_partnership`. Status: `GET /api/uploadposts/status?request_id=`; the URL field
  inside a result is not documented, so the parser accepts `post_url` or `url`.
  **TikTok branded content vs private (verified 2026-11-23, resolves the Phase 1 D open question):**
  - TikTok content sharing guidelines (https://developers.tiktok.com/docs/en/content-sharing-guidelines, last updated
    2026-08-04): branded content "can only be configured with visibility as public/friends". With "only me", the
    Branded Content option must be disabled, or visibility switched to public. Unaudited API clients can post
    `SELF_ONLY` only.
  - Upload-Post (https://docs.upload-post.com/api/upload-video and
    https://docs.upload-post.com/guides/paid-partnership): `paid_partnership=true` + `privacy_level=SELF_ONLY` is
    refused with HTTP 400 `commercial_disclosure_privacy_conflict` before anything is published.
    `disclose_commercial=true` + `branded_content=true` + `SELF_ONLY` is published as `PUBLIC_TO_EVERYONE`, with a
    warning. `paid_partnership` maps to TikTok `brand_content_toggle`, but a sent `brand_content_toggle` (even
    `false`) always wins over it. `brand_content_toggle` and `brand_organic_toggle` default to `false`. Omitting
    `privacy_level` uses the account default, which is public on a public account. A level the account lacks is
    refused with `tiktok_privacy_unavailable`.
  - So private mode sends `SELF_ONLY` + `brand_content_toggle=false` + `brand_organic_toggle=false` and no other
    commercial-disclosure field. `compliance.assert_tiktok_disclosure` runs on the built payload and again on the
    final form just before the HTTP call. Keys are compared case-insensitively, ignoring `_`, `-`, spaces and `[]`,
    at any depth. In private mode it raises `ComplianceError` (invariant 4) for any privacy-level key other than
    exactly `SELF_ONLY`, a missing `privacy_level`, a missing or not-false brand toggle, or any key containing
    `paid_partnership`, `disclose_commercial`, `branded_content` or `your_brand`. Outside private mode it raises
    unless every privacy-level key is exactly `PUBLIC_TO_EVERYONE` and every brand-content toggle is true. The
    dispatcher fails the post locally ("refused before sending").
- `dispatcher.py`: every 30 s; for each slot within the next 60 s on an **armed** evening, publish its posts; then poll
  status every 30 s for up to 10 min; on URL → `posts.state=LIVE`, create `submissions` row with
  `deadline_at = published_at + 30 min` and notify the bot via DB flag. Unarmed at first slot → mark evening skipped,
  return clips to the queue.
- Modes per PRD F9; `dry_run` makes no HTTP calls and fabricates `https://example.invalid/<platform>/<post_id>`.

### 4.8 track
- `reconcile.py`: apply bot/CSV updates to submissions; CSV schema in `schemas.ReconcileRow`.
- `youtube_views.py`: daily `videos.list?part=statistics&id=...` with `YOUTUBE_API_KEY` (1 quota unit per call,
  batch up to 50 ids) → `view_snapshots`.
- `learning.py` (weekly): label posts: positive if paid or ≥ campaign min-payout views within 7 days; negative if
  rejected or < 500 views; write top/bottom 5 to `fewshots`.
- `reports.py`: weekly digest and monthly report (kill/scale rule status) → bot message + `data/reports/*.md`.
- `moments.py` (`cf report moments`): fills `posts.views_24h`/`views_72h` from the view snapshot nearest each mark
  (±6 h; never overwrites), then prints per-archetype performance for re-fitting the moment profile weights.

### 4.9 scheduler (worker)
| Job | When (America/New_York) |
| --- | --- |
| nightly_pipeline | 01:00 daily |
| purge_sources | 03:30 daily |
| backup_db | 04:00 daily |
| youtube_views | 12:00 daily |
| arm_message | every 60 s; sends once per evening between `first slot − arm_message_minutes_before_first_slot` and the first slot (a failed send is retried) |
| slot_dispatcher | every 30 s |
| url_poller | every 30 s |
| submit_reminders | every 60 s |
| weekly_report | Sat 08:00 |
| weekly_learning | Sat 08:15 |
| monthly_report | first Sun 08:00 |
| heartbeat | every 60 s |

### 4.9b pipeline (nightly orchestration, `pipeline.run_once`)
`costs.guard()` → `scan_inbox` → sweeps (expire clips of drained/ended campaigns; recover clips a crashed re-render
left in RENDERED) → render leftover candidates → per source of an active campaign (priority, then oldest; at most
`max_sources_per_night`): download → guard → transcribe → guard → rank → render the new candidates. Stops once
`nightly_candidate_target` clips passed QA in this run; the rest stay CANDIDATE for the next night. Each stage is
isolated: an expected failure is recorded by its module and the run moves on; an unexpected one is rolled back and
alerted. `SpendCapReached` anywhere pauses the run (one pause event, delivered by the bot). Clips are rendered for the
first account set whose platforms the campaign allows (one clip per candidate), into
`media/clips/<slug>/<account set>/r<stamp>/`. A render failure is retried once (next night), then the candidate
expires. A run that produces no clips sends an alert saying why.

### 4.10 costs
`costs.record(service, model, units, unit_kind)` using prices in config; `costs.guard()` raises `SpendCapReached`
when month-to-date ≥ `monthly_api_cap_usd`; pipeline catches it, pauses (`events` row + bot alert).

## 5. Configuration

- Secrets: environment (`.env`), loaded by `pydantic-settings` (`clipforge.config.Secrets`).
- Behaviour: `config/clipforge.yaml` (copy of `clipforge.example.yaml`) → `clipforge.config.AppConfig`.

## 6. External services (official docs to verify against)

| Service | Use | Docs |
| --- | --- | --- |
| Anthropic API | Haiku 5.5 scoring, Sonnet 5.5 copy | https://platform.claude.com/docs/en/models/overview |
| Groq | Whisper transcription | https://console.groq.com/docs/speech-to-text |
| Upload-Post | Publishing | https://docs.upload-post.com/api/upload-video |
| Telegram Bot API | Review UI | https://core.telegram.org/bots/api |
| YouTube Data API v3 | View counts | https://developers.google.com/youtube/v3/docs/videos/list |
| MediaPipe Face Detector | Reframing | https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector/python |

## 7. Deployment

- `Dockerfile`: `python:3.12-slim-bookworm` (linux/amd64), apt `ffmpeg fonts-dejavu-core`, uv sync from lockfile,
  non-root user, `HEALTHCHECK` on the heartbeat file age.
- `docker-compose.yml`: services `worker` (`cf worker`) and `bot` (`cf bot`), same image, `env_file: .env`, volumes
  `./data:/app/data`, `./config:/app/config:ro`, `restart: unless-stopped`, CPU limit 6.
- CI: GitHub Actions on Ubuntu: `uv sync`, `ruff`, `pytest` (no secrets; all external calls mocked).

## 8. Security

- Bot ignores every chat except the owner's. No inbound ports (long polling only).
- Redacting log filter for anything matching configured secret values.
- `.env` git-ignored; Claude Code denied `Read(./.env*)` via `.claude/settings.json`.
