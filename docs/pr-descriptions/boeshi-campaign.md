# Campaign overlays, required caption text, copy style, stream limits; Boeshi campaign (`feat/boeshi-campaign` → `main`)

Written for Carson.

This PR adds the first ClipLaunch campaign, "Boeshi GTA 6 Clipping". **Nothing campaign-specific is in `src/`.** Each
new feature is an optional, generic campaign field. Boeshi itself is only `campaigns/boeshi-gta6.json` plus a
git-ignored overlay image in `campaigns/assets/boeshi/`. The tests use a fake campaign, and no test, prompt or source
file names Boeshi. **The next campaign is one JSON file plus its assets, with zero code changes.** The checklist is in
`docs/CAMPAIGNS.md` under "Adding a campaign".

## What a campaign JSON can now say (all optional; old files load unchanged)

| Field | Effect |
| --- | --- |
| `overlay` `{path, position {x_pct, y_pct}, scale, full_duration}` | A PNG with alpha is composited over every clip. See "Overlay" below. |
| `required_caption_tags` (alias of the existing `required_tags`) | Text every caption ends with, after `#ad` and the copy: hashtags or a link. |
| `copy_style_notes` | The tone from the brief. It goes into the hook/caption prompt as "Style for this campaign: ...". |
| `stream_channels` | Twitch logins the footage may come from. A video from any other channel is refused before any media is fetched. |
| `max_source_minutes` | Download only the first N minutes of a stream source (yt-dlp `download_ranges`). |
| `min_payout_usd` / `min_views_per_post`: `null` | "Not known yet", stored as 0. |

## Contract change (schemas.py, flagged on purpose)

- `CampaignIn` gains the fields above. `required_caption_tags` is folded into `required_tags` by a before-validator,
  and giving both is an error.
- New models `CampaignOverlay`, `OverlayPosition`, `Rect` and `OverlayManifest`.
- `RenderManifest` gains `ad_label_rect` and `overlay`.
- DB migration `0002` adds `campaigns.overlay`, `copy_style_notes`, `stream_channels`, `max_source_minutes` and
  `candidates.hook_history`. It ran on the live DB at `make up`, and a test upgrades a 0001 database that already has
  rows.

## 1. Campaign file

`campaigns/boeshi-gta6.json` holds:
- Rates: $1.00/1K, max $300 per clip, budget $1,750 ($1,618 left); TikTok and Instagram; `game`, `streamer_stream`.
- Footage: `allow_stream_download: true`, `stream_channels: ["darealboeshi"]`, `max_source_minutes: 60`, and the VOD
  `https://www.twitch.tv/videos/2939185858` in `source_urls`.
- Captions: required caption text `twitch.tv/darealboeshi`, and copy style "humor/meme, voice-joke bits, relatable
  captions, chad-edit energy".
- `rules_notes` records the brief rules: overlay visible for the whole clip, 40 %+ tier-1 audience, 0.8 % engagement,
  clips stay live 30 days, no more than 5 near-identical clips, no engagement bait, plus the brief URL.
  - **The Notion brief could not be read by Claude:** the connector returned 404 and the public page is JS-only. The
    rules come from Carson's summary, and `rules_notes` says so.
  - Min payout, participants and approval rate have not been read.
  - Account-level rules (tier-1 %, engagement %), the 30 days live and the near-identical limit are recorded but not
    enforced in code. Nothing deletes posts, so the 30-day rule holds by default.

## 2. Overlay (`render/campaign_overlay.py`, `render/encode.py`, `render/clip.py`, `compliance.py`)

- **The image:** `Boeshi Overlay.png` is 2172×724 RGBA. The margins are fully transparent. The banner (purple Twitch
  glyph plus "@darealboeshi" on black) is opaque in the box x 170–2002, y 148–588.
- **Placement** (JSON: centred, `y_pct 31.5`, `scale 0.6`): the image is 648×216 px. **Its visible part covers
  x 266–814, y 541–673** of the 1080×1920 frame. That sits in the blurred band between the hook and the gameplay, and
  overlaps the top ~17 px of 16:9 gameplay.
  - It is clear of TikTok's UI zones: top 150 px, right 15 % (x > 918), bottom 20 % (y > 1536).
  - It is clear of the caption band (y 1134–1401, the worst case over every style preset) and the hook's worst-case
    box (y 166–525, 3 lines at the largest size), with an 8 px margin.
  - The first proposal (`y_pct 31`) failed the hook check by 2 px. Per Carson's rule I stopped and asked, and Carson
    chose 31.5.
- **Checks:**
  - `cf campaign add` and every render check that the file exists under `campaigns/assets/` (symlinks resolved), is a
    PNG with an alpha channel, and that its visible pixels miss the captions, the hook, the frame edge and the UI
    zones.
  - Any conflict refuses with every reason listed. **The code never shrinks or moves the overlay.**
- **"Ad" label:** it is placed at the first fixed top spot (top-left, top-right, top-centre) clear of the overlay's
  visible pixels, or the render is refused. Boeshi's overlay leaves it at top-left (48, 120). It is drawn on top in
  any case: the overlay is composited **before** `ass`.
- **ffmpeg:** input 1 is the PNG, opened with `-an -sn -dn`: `[1:v]scale,format=rgba` then
  `overlay=x:y:eof_action=repeat`. The single frame stays up for the whole clip. With `full_duration: false` it is
  shown only after the hook.
- **Manifest:** it records the overlay rects and timing plus the label rect. `compliance.overlay_problems` (QA and
  `assert_render_manifest`) fails a clip whose overlay covers the label, or whose full-duration overlay does not span
  0 to the clip end.
- **Invariant 10:** `maps_only_source_audio` allows one extra input, only if it is a plain local `.png` opened with
  `-an -sn -dn`. Filtergraph input pads may only be `[0:v]`/`[1:v]`, which also catches unlabeled `[1]`, `[1:1]` and
  `[0]`. `-f` and `-loop` are forbidden. Audio is still mapped only from `0:a`.
- **TOCTOU:** the overlay is re-hashed right before ffmpeg runs, and the render is refused if it changed.
- `campaigns/assets/` is git-ignored. `docker-compose.yml` mounts `./campaigns` read-only into both containers.
- `docs/COMPLIANCE.md`: invariant 3 now covers overlays, and invariants 1, 2, 9 and 10 name their new tests.

## 3. Captions (`compliance.normalize_caption`, `rank/prompts.py`, `prompts/clip_copy.md` v2)

- **Final caption** = `#ad` + copy + required caption text, **always last** and in the campaign's order.
  - A required tag the copy already used is moved to the end, not repeated. A longer link that contains a URL tag
    (`https://twitch.tv/...`) is dropped whole.
  - Example: `#ad <copy with 2–4 hashtags> twitch.tv/darealboeshi`.
- **Invariant 9:** a link can't go on X. `normalize_caption` raises on X, and `cf campaign add` refuses a campaign
  that lists `x` with URL-like caption text. On main, such a tag would have been added to X captions after the URLs
  were stripped. This closes that gap.
- **Prompt v2:**
  - It follows the campaign's style note when one is given (humor and meme allowed), keeps 2–4 hashtags, and is
    explicit about nothing misleading and no engagement bait.
  - It never writes the required tags or links itself (the system adds them), and it gets an avoid-list of hooks
    already used.
  - A test checks the prompt contains no word from any campaign file's name, tags or channels.

## 4. New hook never repeats

- `write_copy(avoid_hooks=...)`: every hook the clip has had (`candidates.hook_history` plus the current one) is sent
  to the model in `<used_hooks>`.
- A reply that matches one, ignoring case and punctuation, is retried once with a reminder. If it repeats again, the
  re-render stops ("new hook failed (RepeatedHookError ...)"), and the current version goes back for review unchanged.
- On success the old hook is appended to `hook_history`. A New hook costs at most 2 Sonnet calls.

## 5. Twitch: channel-checked, first N minutes (`ingest/twitch.py`)

- With `stream_channels`:
  - yt-dlp fetches the metadata first, with no media. A video from another channel, or a VOD still recording, is
    refused before anything is downloaded.
  - The downloaded result is checked again, and the file is deleted on a mismatch.
  - Verified in the yt-dlp 2026.10.26 source: a VOD's channel login is `uploader_id` (GQL `owner.login`). For clips,
    `uploader_id` is the clipper, so the channel display name is compared lowercased; a mismatch refuses, which is
    the safe direction.
- With `max_source_minutes`: `download_ranges` = `[0, N·60]`. The callback shape is from the `YoutubeDL` docstring.
- Without either field: one call and the whole video, exactly as before.

## Run (private mode; nothing armed or published)

`make up` rebuilt both services; both were healthy and both logs said `publish mode private`. Migration 0002 ran on
the live DB.

1. `docker compose exec worker cf campaign add --from-json campaigns/boeshi-gta6.json` registered campaign 2 and
   source 2. It printed: "overlay ...: visible area x 266-814, y 541-673 (clear of captions, hook and platform UI);
   'Ad' label at (48, 120)".
2. `docker compose exec worker cf run --once`:
   - **Twitch did not block anything.** yt-dlp fetched the metadata (channel `darealboeshi` matched), then downloaded
     exactly the first 3600 s in about a minute.
   - Groq transcribed it (3600 s billed, $0.04).
   - Haiku scored and Sonnet wrote the copy; 8 candidates were rendered, **0 failed QA, 8 ready for review**.
   - The bot sent all 8 previews (clips 8–15, between 02:29 and 02:34 UTC).
3. A read-only check of the 8 real clips (app code inside the worker, numbers only):
   - Every manifest has the overlay over 0 → end of the clip and the "Ad" label rect at (38–130, 110–190).
   - Sampled pixels inside the banner match at 0.5 s and at the last frame: the black panel is (0, 0, 0) and the
     purple panel (145, 82, 255).
   - The "Ad" label box changes between 0.5 s and 4 s on every clip (mean change 9–100, depending on the background).
   - Every TikTok and Instagram caption starts with `#ad ` and ends with `twitch.tv/darealboeshi`, with 2–4 hashtags
     before it.
4. Spend: the month to date went from $0.08 to $0.17 of the $15 cap.
5. `make up` again after the last guard fix (the space/hex pad labels), so the containers run this PR's final code.

Nothing was armed, approved or published, and `PUBLISH_MODE` and `.env` were not touched.

## Verification

- `make lint test`: ruff clean; 1278 passed, 12 skipped, 1 deselected.
- `mypy --strict` on `schemas.py` and `compliance.py`: clean.
- In the `clipforge:latest` image (Debian ffmpeg 5.1): `tests/render`, `tests/qa` and `tests/compliance` gave
  444 passed, 0 skipped.
  - The real overlay render: magenta box mean grey diff 120 at 0.5 s, 6 s and 11.5 s; "Ad" label box diff 0.0 versus
    a render without the overlay.
  - The existing label test is unchanged: 49 at 0.5/2.5 s, 0.0002 at 4 s.
- compliance-reviewer, pass 1: **FAIL**, on one procedural item. Every invariant was preserved, and 1, 2, 3, 9 and 10
  were stricter. The FAIL: three tests named in the COMPLIANCE.md table lived outside `tests/compliance/`.
  - Fixed: they moved into `test_inv01_sources.py`, `test_inv09_x_and_political.py` and `test_inv03_ad_label.py`,
    with a note that the real-render one counts only with ffmpeg.
  - Its hardening suggestions were applied:
    - the filter-pad check and `-an -sn -dn` on the overlay input
    - the overlay re-hash before ffmpeg
    - whole-link removal
    - a note on Twitch clip display names
- compliance-reviewer, pass 2: **PASS** for invariants 1–12. All 36 tests named in the COMPLIANCE.md table are under
  `tests/compliance/`, and `git diff main -- tests/compliance` only adds lines.
  - Its remaining non-blocking gap is fixed: pad labels written with a space or in hex (`[ 1:a]`, `[0x1]`) slipped past
    the pad check. Now any label that starts with a digit after spaces must be exactly `0:v`/`1:v`, and those cases are
    in `test_overlay_input_pads_and_flags`.
  - It asked me to note two things. The contract (`schemas.py`) changed, as described above. And
    `allow_stream_download: true` rests on Carson's summary of the brief (the brief names Boeshi's recent Twitch VODs
    as the footage), because Claude could not read the Notion page.
- No test in `tests/compliance/` was weakened; they only gained tests. `tests/test_campaign_files.py`'s rule "required
  tags start with #" now also accepts a link on a campaign without X (the reviewer judged this acceptable).
- Existing tests updated on purpose: prompt version `clip_copy@1` → `@2` in two places, the prompt-variables test
  (`avoid_hooks`), and `test_repo_campaign_files_register`.
  - That last test now expects `allow_stream_download` to match the file.
  - In CI, the overlay image is git-ignored, so the test expects a campaign whose overlay image is missing to be
    refused, never registered half-configured.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
