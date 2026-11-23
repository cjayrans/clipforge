# PR: `feat/ingest-transcribe` → `main` (Phase 1 A)

This branch adds the campaign registry, source registration and download (invariant 1), the `inbox/` drop folder, Drive
folder links, audio chunking, Groq transcription, the no-speech guard, the faster-whisper fallback and the source purge
job. It changes only `src/clipforge/ingest/`, `src/clipforge/transcribe/`, their tests, and two lines in two shared test
files (listed under "Shared files touched").

## Tasks done

| Task | What | Main files |
| --- | --- | --- |
| A1 | Campaign registry, `cf campaign add/list/drain/end/check` (details below) | `ingest/registry.py` |
| A2 | Download with invariant 1, SHA-256 and ffprobe (details below) | `ingest/download.py`, `ingest/media.py` |
| A2c | Twitch via yt-dlp, refusals of YouTube/Kick/other hosts, and the `inbox/` drop folder (details below) | `ingest/twitch.py`, `ingest/inbox.py` |
| A2b | Drive folder links register each media file as its own source; Dropbox folders are refused (details below) | `ingest/folders.py` |
| A3 | Audio extraction, chunk planning and overlap merge (details below) | `transcribe/audio.py`, `transcribe/chunker.py` |
| A4 | Groq client and `transcribe_source`, producing `schemas.Transcript` (details below) | `transcribe/groq_client.py`, `transcribe/service.py` |
| A4b | No-speech guard (details below) | `transcribe/guard.py` |
| A5 | faster-whisper fallback when Groq fails twice (details below) | `transcribe/local.py` |
| A6 | Purge job (details below) | `ingest/purge.py` |

Every source status change writes an `events` row (`ingest.models.set_source_status`). Operator alerts are `events`
rows with `entity="alert"` (`ingest.alerts`); see CR-A1 for how they reach Telegram. URL query strings (signed links,
Dropbox `rlkey`) are stripped from alert text.

### A1: Campaign registry
- `add_campaign_from_json`:
  - Validates the JSON against `schemas.CampaignIn`.
  - Upserts by slug. An update never changes status or `created_at`.
  - Registers the campaign's `source_urls`.
- Status changes: active ↔ drained, and active or drained → ended. Ended is terminal.
- `cf campaign check` applies `config.campaign_filter`, one rule from docs/CAMPAIGNS.md per check.
- Errors the operator can fix exit 1 with `Error: …` instead of a traceback.

### A2: Download
- `download_source` calls `compliance.assert_registered_source` first, every time, whatever the source's status.
- Direct links:
  - httpx streaming download.
  - tenacity retries on transport errors, 429 and 5xx, with exponential backoff and at most 3 attempts.
  - Writes a `.part` file, then renames it into place.
  - Refuses HTML responses.
  - Refuses redirects to YouTube, Kick, Twitch, and every platform or marketplace compliance refuses (TikTok,
    Instagram, X, Facebook, Content Rewards, Vyro). See CR-A6.
- Dropbox file links are rewritten to `dl=1`.
- Drive file links go through a `gdown` adapter.
- After download:
  - Footage with no audio stream: FAILED `no_audio` (PRD F17).
  - A file identical to another source of the same campaign: FAILED `duplicate_of:N`.
  - Error contract: see CR-A5.

### A2c: Twitch, refused hosts and inbox
- Twitch:
  - yt-dlp runs only when `allow_stream_download` is true, and only with the `twitch:vod` and `twitch:clips`
    extractors.
  - A result from any other extractor is deleted and refused.
- YouTube and Kick are refused at `cf source add` (with an alert) and again in the downloader.
- `scan_inbox` registers media files in `inbox/<slug>/` as `file://inbox/<slug>/<path>`, for active campaigns only.
- `take` moves a file into the media dir. It refuses:
  - paths that escape the folder,
  - another campaign's folder,
  - symlinked files and symlinked campaign folders.

### A2b: Folder links
- A Drive folder is listed with `gdown.download_folder(skip_download=True)`, recursively.
- Each media file is registered as `https://drive.google.com/file/d/<id>/view` with `folder_url` set.
- Re-adding a folder only adds the new files.
- Dropbox folder links are refused with guidance: register each file's link, or use `inbox/<slug>/`. This was your
  decision in planning, because listing one needs an API token.

### A3: Audio and chunks
- Audio is extracted as mono 16 kHz MP3 at 32 kbps, first audio stream only.
- `plan_chunks` (pure):
  - Each chunk is under 24 MB with a 0.9 safety factor, about 94 minutes.
  - Consecutive chunks overlap by exactly 2 s.
- `merge_chunks` (pure):
  - Splits each overlap at its midpoint.
  - Drops duplicate words at the seam.

### A4: Groq transcription
- Groq client:
  - The SDK's own retries are off (`max_retries=0`); tenacity retries instead, honouring `retry-after`.
  - `costs.guard()` runs before every request.
  - `costs.record()` runs after every success, with the 10 s billed minimum.
- `transcribe_source` writes:
  - `media/transcripts/<id>.json`,
  - a `transcripts` row with the cost,
  - the source status TRANSCRIBED.

### A4b: No-speech guard
Below 20 words per minute:
- The paid-for transcript is still stored.
- The source becomes FAILED with `fail_reason="no_speech"`.
- An operator alert is written.

### A5: Local fallback
- Groq "fails twice" means one chunk fails two times in a row, and each failure is already 3 retried attempts. A
  missing `GROQ_API_KEY` also triggers the fallback.
- On fallback, the whole source is transcribed locally on CPU (int8), giving one consistent transcript, and a
  `transcribe_fallback` alert is written.
- The flag is stubbed until CR-A2 lands.

### A6: Purge
- A source is purged 7 days (`retention.source_days_after_terminal`) after all its candidates are terminal: either the
  candidate itself, or all of its clips.
- FAILED sources, and RANKED sources with no candidates, also count as done.
- Only files inside `media_dir` are deleted. The sha256 and `fail_reason` are kept.

## Tests added (178 new; suite: 388 passed, 3 skipped, 1 deselected (slow), 21 xfailed)

| File | Count | Covers |
| --- | --- | --- |
| `tests/ingest/test_registry.py` | 41 | Repo campaign files load. Upsert. Bad JSON and schema errors. Status transitions. Each filter rule fails on its own. Every source-refusal reason, with an alert. Alert text drops URL query strings. |
| `tests/ingest/test_cli_campaign.py` | 2 | Real `cf` commands end to end: refusal alerts are committed; errors exit 1. |
| `tests/ingest/test_download.py` | 46 | Refused hosts make **no request** (respx catch-all). Inactive campaign. Direct download: hash, `.part` file, events. Retries. 429 exhaustion. 404 not retried. HTML refused. Redirects to YouTube, Kick, Twitch, TikTok, Instagram, X, Facebook, Content Rewards and Vyro refused, with the target never requested; CDN redirects followed. Size cap. No audio. Unreadable media. Duplicates. Dropbox `dl=1`. Drive adapter. |
| `tests/ingest/test_twitch_inbox.py` | 22 | yt-dlp is never constructed for YouTube, Kick or unpermitted Twitch. Options are pinned. A non-Twitch extractor result is discarded. The extractor names exist in the installed yt-dlp. Inbox scan, move and hash. Traversal, other-campaign, symlinked-file and symlinked-folder refusals. |
| `tests/ingest/test_folders.py` | 13 | Drive listing (nested, non-media skipped, idempotent re-add). Listing failure alerts. Dropbox folder refusal with guidance. |
| `tests/ingest/test_purge.py` | 10 | Exactly 7 days. Pending candidate or live clip blocks the purge. Mixed clips. FAILED sources. Files outside media_dir are never deleted. Idempotent. |
| `tests/transcribe/test_chunker_audio.py` | 14 | Hypothesis properties: each chunk stays under the byte limit, overlaps are exactly 2 s, and merge rebuilds each word once. Seam dedupe. ffmpeg args. Real ffmpeg (skipped without ffmpeg). |
| `tests/transcribe/test_groq.py` | 15 | Wire format (see the Groq facts below). Cost and the 10 s minimum. Retries (503, then 429 with `retry-after`, then 200). Exhaustion. 401 not retried and the key never appears in the error. Spend cap checked before the call. Single- and multi-chunk `transcribe_source`. |
| `tests/transcribe/test_no_speech.py` | 7 | Both fixture transcripts (sparse ~10.7 wpm fails, talk ~141 wpm passes). Threshold edges. FAILED `no_speech` with an alert. |
| `tests/transcribe/test_local_fallback.py` | 8 | Covers the fallback cases below, plus a slow `tiny`-model test (skipped without faster-whisper or ffmpeg). |

`test_local_fallback.py` cases:
- One Groq failure is retried and does not fall back.
- Two failures fall back to local for the whole source.
- No Groq key goes straight to local.
- The fallback can be disabled.
- When faster-whisper is unavailable, the error reports both failures.
- faster-whisper output is mapped to `Word`/`Segment`.

All external HTTP is mocked with respx: Groq (through the SDK's httpx transport), direct links and Dropbox. `gdown` uses
`requests` and yt-dlp has its own networking, so both are replaced by fakes in tests. No test makes a network call or
sets `PUBLISH_MODE`.

## External facts verified (2026-11-22)

**Groq speech-to-text.** Sources: https://console.groq.com/docs/speech-to-text, https://console.groq.com/docs/api-reference,
https://console.groq.com/docs/rate-limits and https://console.groq.com/docs/errors, plus the installed `groq` 1.7.0 SDK.
- **Request**
  - `POST https://api.groq.com/openai/v1/audio/transcriptions`, multipart, `Authorization: Bearer`.
  - Word timestamps need `response_format=verbose_json` and `timestamp_granularities[]` set to `word` and `segment`.
  - The docs' code comment that says `json` contradicts the parameter table and the API reference; it is a typo.
  - The SDK sends the field as `timestamp_granularities[]`, and a test asserts it on the wire.
  - `parse_verbose_json` refuses a response without `words`.
- **Response**
  - Words carry only `word`, `start` and `end`, with no probability, so `Word.prob` is `None` for Groq.
  - Segments carry `start`, `end` and `text`.
  - Top-level `language` and `duration` are not documented, so they are optional. Duration comes from ffprobe.
- **Limits and pricing**
  - Max file 25 MB on the free tier, 100 MB on dev. Billed minimum 10 s per request.
  - `whisper-large-v3-turbo` costs $0.04 per audio hour, matching the config price.
  - Dev-plan limits: 20 requests per minute and 7,200 audio-seconds per hour.
  - A 429 carries `retry-after`.
  - No charge for server errors.
- **Not verified:** the free-plan rate-limit column (not in the extracted page).

**gdown.** Checked against the installed gdown 6.5.2 source (https://github.com/wkentaro/gdown).
- `download(url=..., output=<dir>/)` parses `/file/d/<id>/view` links itself; there is no `fuzzy` argument in this
  version.
- `download_folder(url=..., skip_download=True)` returns `GoogleDriveFileToDownload(id, path, local_path)` for every
  file, recursing into subfolders, and creates no directories.
- No file-count cap in the code.

**Dropbox.** Source: https://help.dropbox.com/share/force-download.
- `dl=1` forces a direct download of a shared file link.
- **Not verified:** any way to list a shared folder without an API token. That is why Dropbox folder links are
  refused (A2b).

**yt-dlp.** Checked against the installed 2026.10.26 source (the `YoutubeDL` docstring and `_load_extractors`) and
https://github.com/yt-dlp/yt-dlp.
- `allowed_extractors` is a list of regexes matched with `re.fullmatch`, case-insensitive. The extractor names
  `twitch:vod` and `twitch:clips` exist; a test checks this.
- The downloaded file path is in `info["requested_downloads"][0]["filepath"]`.
- **Not verified:** whether Twitch VODs ever need an ffmpeg merge. I use `format=best[ext=mp4]/best` (muxed HLS) to
  avoid one, and ffmpeg is in the image anyway.

**faster-whisper.** Sources: https://github.com/SYSTRAN/faster-whisper (README) and `faster_whisper/transcribe.py` on
master.
- `WhisperModel(size, device="cpu", compute_type="int8")`. `large-v3-turbo` and `tiny` are valid size names.
- `transcribe(path, language, word_timestamps=True)` returns a lazy segments generator and an info object.
- `Word` has `start`, `end`, `word` and `probability`; `Segment` has `start`, `end`, `text` and `words`.
- Audio is decoded with PyAV.
- **Not verified:** a PyPI release tag. It cannot be installed on the Intel Mac; the slow test runs it in Docker/CI.

## Compliance review (`compliance-reviewer` subagent)

- **First pass: FAIL** on invariant 1.
  - A direct link that redirected to TikTok, Instagram, X, Facebook, Content Rewards or Vyro would have been
    followed. Only YouTube, Kick and Twitch redirects were blocked.
  - Fixed in commit `19b1a5a`: those redirects are now refused, the target is never requested, and the source ends
    FAILED `host_refused`.
  - Tests: `test_redirect_to_refused_platform_refused` (9 hosts) and `test_is_refused_platform_host`.
  - `compliance.py` is unchanged; the public helper is CR-A6.
- **Hardening suggested by the reviewer, all applied:**
  - Symlinked `inbox/<slug>` folders are refused.
  - URL query strings (signed links, Dropbox `rlkey`) are stripped from alert text and source event details.
- **Second pass: PASS.**
  - Invariant 1: preserved.
  - Invariant 12 (spend cap): preserved. `costs.guard` runs before every Groq call.
  - Invariants 2–11: untouched.
  - No secrets logged, no publish or engagement code, `PUBLISH_MODE` never set.
  - `tests/compliance`: 119 passed, 21 xfailed.

## Shared files touched (merge notes)

- **`tests/test_cli.py`**
  - Removed the six "Phase 1 A not implemented" parametrize cases.
  - `test_every_target_is_known_to_be_missing_now` now skips `ingest.*` targets.
  - You approved this edit in planning. **Expect a conflict with `feat/ops`**, which has to make the same kind of
    edit for its targets. Keep both sides' removals.
- **`tests/compliance/test_inv01_sources.py`**
  - Removed only the `xfail_until` marker on `test_downloader_refuses_youtube_source`, which now passes.
  - Also removed the now-unused `xfail_until` import, so ruff stays clean.
  - Nothing else in `tests/compliance/` changed.
- Not edited: `pyproject.toml`, `uv.lock`, `schemas.py`, `db/`, `config.py`, `compliance.py`, `cli.py`, and every
  other section's folders.
- **`docs/TASKS.md`** checkboxes were left unticked to avoid a four-way conflict. Please tick A1–A6 in P2.1.

## Contract requests (docs/contract-requests/ingest-transcribe.md)

- **CR-A1:** a delivery path for operator alerts (`events` rows with `entity="alert"`) to Telegram, plus a
  "sent" marker.
- **CR-A2:** a `transcribe:` config block: `local_fallback`, `local_model`, `language`. Stubbed with defaults.
- **CR-A3 (optional):** `cf campaign check` should exit 1 when a check fails.
- **CR-A4:** fix the `ingest/__init__.py` docstring signatures. `cli._delegate` always passes `settings`.
- **CR-A5:** pipeline wiring notes for P2.2 (see "Left for Phase 2").
- **CR-A6:** a public `compliance.is_refused_platform_url`. The redirect check stubs it with the public
  `classify_source_host`.

## Left for Phase 2

- **P2.2 pipeline**:
  - Call `scan_inbox`, then `download_source`, then `transcribe_source`.
  - Commit the session after catching `ComplianceError`, `DownloadError` or `TranscriberError`; the status and alert
    are already flushed.
  - Rank only TRANSCRIBED sources.
  - Let `SpendCapReached` pause the pipeline.
- **Scheduler**: `purge_sources` at 03:30.
- **Bot**: send alert events (CR-A1).
- **Campaign drain/end**: expire unpublished candidates and clips (ARCHITECTURE §2).
- **Optional**: a `cf source retry <id>` command for FAILED sources.
- **Real-tool tests**: the ffmpeg/ffprobe tests and the slow faster-whisper test skip on the Mac. They run in
  Docker/CI.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
