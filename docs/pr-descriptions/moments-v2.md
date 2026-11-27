# Moments v2: pick clips by content and viewer response, scan whole VODs (`feat/moments-v2` → `main`)

Written for Carson.

The moment ranker used to find clips by sound: a loud trigger plus a reaction within 3 s, or no candidate at all.
Quiet footage was dropped before any model saw it, and Haiku scored four things from the clip's own transcript only.
This PR changes how moments are found, scored and checked:

- **Finding:** by content first. Haiku reads the whole transcript.
- **Scoring:** on a six-part rubric aimed at watch time, completion and sends.
- **Checking:** the shortlist gets a keyframe check.
- **Scanning:** whole VODs, with chunk boundaries handled safely.
- **The night:** sources compete with each other for the night's review slots.

Dylan Page and AsianJeff now use the moment profile too. The minimum score is still 40.

## 1-3. Catalogue, rubric, window (`config/moment_profiles/gta6-streamer.yaml`, `weights_version: gta6-streamer-v2`)

**Moment catalogue.** Your seven types, each with examples and signs:

- `blunder`: the streamer screws up and reacts.
- `npc_absurd`: something absurd with an NPC.
- `glitch`: a glitch or physics fail.
- `chaos`: chaos that escalates fast.
- `discovery`: finding something hidden. Its copy hint allows "secret / hidden" framing, and Sonnet gets it.
- `bit`: a roleplay or deadpan bit that lands.
- `chat_interplay`: chat and the streamer play off each other.
- `other`: anything else.

Every prompt says the catalogue is *examples of what good looks like, not a list of allowed moments*, and `other` is
always allowed. A test checks that a moment no archetype describes is proposed, scored and picked
(`test_an_other_moment_outside_the_catalogue_survives_selection`).

**Reject list.** Your six items: walking or driving with nothing happening; chat or settings talk; menus and loading;
long cutscenes, endings and reveals; jokes needing two minutes of context; loud reactions to nothing.

**Rubric** (0-10 each), defined in the YAML:

| criterion | weight |
|---|---|
| hook | 0.2 |
| arc | 0.2 |
| arousal | 0.2 |
| sendability | 0.2 |
| clarity (standalone) | 0.1 |
| density / continuity | 0.1 |

Weighted score = 10 × Σ weight × score.

**Window rule** (replaces the 2-5 s pre-roll):

- Open on the most interesting beat a stranger can still follow; the hook text gives the context.
- At most 0.6 s of dead air at the start.
- End exactly 1.5 s after the payoff, on a word edge only if it's within 0.4 s.
- Aim for 15-30 s, with 12 s minimum and 45 s hard cap. A clip over 30 s starts at the ranker's first beat
  (`first_beat_s`, `score_moments@3`).
- If the payoff runs past the window, the window is **extended**. A moment that can't fit 45 s is dropped, never cut.

**Kept from v1:**

- The Rockstar skip rules. They gain `no_event` (menu / loading / just chatting).
- The sexual/graphic flag: review only, never an auto-reject. The content and frame prompts now say this too.

## 4. Content-first finding (`rank/content_pass.py`, `rank/candidates.py`)

- **The content pass.** The whole stitched transcript goes to Haiku (`propose_moments`) in 12 min segments with 2 min
  overlap. It returns start, end, trigger, payoff, archetype or `other`, confidence and a reason, quiet and deadpan
  moments included. Proposals are deduplicated on the global timeline, and a whole proposal beats one cut at a segment
  edge.
- **Signals.** Loudness, motion, exclamations and reaction pairing still propose candidates, including strong
  reactions on their own. They are evidence only. The `dead_footage` and trigger+reaction gates are gone.
- **The new content gate.** The ranker must see a clear trigger AND a clear payoff.
- **Merging.** The two finders are merged by overlap, and every candidate records `found_by`: content, signals or
  both.
- **Context.** The ranker sees 30 s of transcript either side of each candidate.

## 5. Vision check (`rank/vision.py`)

- **What gets checked.** The best 12 survivors, plus any later pick that moves up when a checked one drops (budget 24),
  so every final pick has been checked.
- **What is sent.** About one keyframe per 2.5 s (max 8) at 768 px wide, plus two facecam crops when a facecam is
  found. The facecam is detected on the shortlist windows first and stored.
- **What comes back** (Haiku, `judge_frames`):
  - whether an event, the payoff and the reaction are visible
  - the scene: gameplay, menu/loading, cutscene, just chatting or mixed
  - spoiler risk, graphic risk, and whether the setup is clear
  - corrected rubric scores, which replace the transcript scores
- **What it can trigger:**
  - Menu or chatting with no event → `no_event` skip.
  - A cutscene with no visible reaction → `cutscene` (reject); with a reaction → `cutscene_part` (×0.6).
  - Spoiler → `story_reveal`.
  - Graphic → review flag.
- **Settings:** `vision.enabled`, `shortlist`, `max_frames`, `frame_every_s`, `frame_width`.
- **Cost.** It goes through both spend caps, and its cost is recorded per candidate. Source 2: about **$0.001 per
  candidate**.

## 6. Whole VODs

**How `max_source_minutes` worked.** It downloaded the **first N minutes**: yt-dlp `download_ranges` of
[0, N×60] s, Twitch only. It did not sample across the VOD. With Boeshi's 60, the night only ever saw the first hour.

**Changes:**

- `max_source_minutes` is removed from `campaigns/boeshi-gta6.json`. The field still exists for campaigns that need
  it.
- Groq chunks are 30 min with 45 s overlap (see 7).
- Long ffmpeg passes get a 4 h timeout instead of 1 h.
- New `per_source_cap_usd` (default $1.50): `costs.for_source` tags every cost row with source and stage. At the cap
  that source stops with an alert; the run goes on.
- New disk check before every download: the VOD's size from yt-dlp metadata plus `min_free_gb` (15). Otherwise skip,
  alert, and try again next night.
- A RANKED source's footage is deleted as soon as nothing is pending: no clip awaiting review, approval or publishing,
  and no unrendered candidate. Unrendered candidates expire 3 days after ranking. FAILED sources keep their file for
  `cf source retry`, and the 7-day purge stays as the backstop.

**Full-VOD numbers (source 2, 4.77 h, final code):**

| | |
|---|---|
| Download | 12.30 GB in 9.0 min |
| Peak disk | about 25 GB (yt-dlp writes a remuxed second copy; see 10) |
| Transcribe | 3.7 min, $0.20 |
| Signals | 5.8 min |
| Content pass, scoring and vision | 7.6 min |
| Total | 26 min wall time, $0.28 |

`RunReport` now prints one line per source with file size, free disk, stage times and spend.

## 6b. Why the second run failed, and the stall watchdog

**Diagnosis**, from `pmset -g log` and the container and worker logs:

- **01:52 UTC:** the Amphetamine single-use session released its "prevent system sleep" assertion.
- **01:58:39 UTC:** the Mac entered idle sleep, 13 min into the run.
- **13:20:06 UTC:** the Mac woke (lid open).
- **The same second:** the fixed 4 h ffmpeg timeout fired. The clock had jumped 11 h while the Docker VM was paused,
  so a healthy audio extraction was killed the moment the machine woke.

So it was not slow Twitch, and not a code bug in the pipeline steps. The fixed timeout made it worse by counting
sleep as runtime. The **live worker** was hit too: it logged `nightly_pipeline`, `purge_sources` and `backup_db` as
"missed by 7:20" on wake, so **last night's run did not happen**.

**Fix** (`ingest/watchdog.py`):

- **Active time.** Time is counted by a ticker thread that skips host sleep and VM pauses.
- **ffmpeg.** Every ffmpeg call through `media.run` reports `-progress`. With no output growth for 10 min of active
  time, the step is killed and retried, at most 2 retries.
- **Twitch download.** A progress hook checks for no new bytes for 10 min, and retries the download with the
  channel, live and disk checks run again first.
- **Stage ceilings** of active time, about 3x the first full-VOD run:

  | stage | ceiling |
  |---|---|
  | download | 40 min |
  | transcribe | 15 min |
  | rank | 35 min |
  | copy | 10 min |
  | whole source | 90 min |

  Groq and Claude calls check the ceiling before each request.
- **Failure handling.** A stall or ceiling marks the source FAILED, with an alert asking for `cf source retry`.
  Phase A starts a new source only within a 60 min budget, so a night's ranking can't run for hours.
- **Tests:** a simulated 11 h sleep counts as one tick; a silent step is retried then fails; the ceiling kills a
  running step; a stalled download is retried; a stage timeout fails the source while the night goes on.

## 7. Chunk-boundary safety

**What I found:**

- **Groq audio:** about 94 min chunks with a **2 s** overlap at fixed cuts. A word at a cut could be lost or doubled.
- **Signals:** already computed over the full file.
- **Clips:** already cut by timestamp from the full source.
- **Ranker batches:** already by candidate, but each candidate saw only its own window (no context).

**Now:**

- (a) Groq chunks overlap by 45 s. One `silencedetect` pass moves cuts into silences and puts each stitch seam in a
  silence inside the overlap. Words go onto one global timeline, each word once.
- (b) Signals, the content pass and candidate pairing run on that global timeline.
- (c) Windows are decided on the global timeline, and clips are cut from the full file.
- (d) Batches are 8 candidates each, with ±30 s of context per candidate.
- (e) A window extends to include its payoff.

**Tests** (`tests/rank/test_boundaries.py`). Each of these comes out as **one** candidate with its setup and payoff:

- a moment straddling a Groq chunk boundary
- a moment cut by a content-segment edge
- a moment whose setup sits in another scoring batch's window

## 8. Candidate pool depth

**Before:**

- Nothing found new VODs; you registered each one by hand.
- Two sources a night, but the first could fill all 8 review slots alone, so the second never competed.

**Now:**

- **VOD proposals.** A nightly job (00:30) uses Twitch's **official Helix API** to list recent archive VODs of each
  campaign's `stream_channels`. Field names are verified at dev.twitch.tv/docs/api/reference/videos, users, streams
  and the client-credentials page.
  - It skips live, older-than-`recent_vod_days`, registered and already-proposed VODs.
  - At most 2 per campaign per night are sent to Telegram with **Add / Dismiss**.
  - **Nothing is registered automatically.** Add goes through `registry.add_source`, the same checks as
    `cf source add`, and re-checks the age window at tap time.
  - Only campaigns with `auto_propose_vods`, `allow_stream_download` and `recent_vod_days` are looked up.
- **Cross-source nights.**
  - **Phase A** ranks every source of the night first (download → transcribe → content pass → scoring → vision), with
    no copy.
  - **Phase B** renders the best candidates across all sources until 8 pass QA.
  - Sonnet copy is written only for those, right before they render.
- **Thresholds.** `min_score` is **unchanged at 40**. `gates.max_candidates` went from 40 to 150, because a whole VOD
  gives about 100 content proposals and 40 cut most of them unscored. Scoring 150 costs about $0.05.

## 9. Calibration

- **Candidates** store, in migration 0005:
  - the six rubric scores, as columns plus `scores_json`
  - `found_by`, `content_confidence` and `vision_json`
  - the archetype and weights version, as before
- **Posts** gain `avg_watch_time_s`, `completion_rate`, `shares` (TikTok shares / IG sends), `likes` and
  `metrics_at`. 24 h / 72 h views already existed.
- **`cf post metrics <post_id>`** takes `--watch-time`, `--completion` (0-1 or %), `--shares`, `--likes`,
  `--views-24h` and `--views-72h`, and writes an events row.
- **`cf report moments`** keeps the per-archetype table. Once 20 posts have completion or share data, it also shows
  Spearman correlations of each rubric score with completion and with shares per view: overall, per archetype and per
  platform, with n per cell.
- **The Telegram preview** shows the six scores, found_by, the frames verdict and the CHECK BEFORE APPROVING line.

## 10. Source 2 with the final code

This is the full report from `cf source shadow-rank 2 --full --compare-clips 24-31`:
[moments-v2-source2-shadow.md](moments-v2-source2-shadow.md). It ran on 27 Nov, 14:20-14:46 UTC, in a one-off
container on a copy of the database, under `caffeinate -dimsu` with a 90 min wall-clock cap. No candidates were
written.

**Top 10, with the six scores in order hook arc arousal sendability clarity density:**

| # | window | score | archetype | scores | found by | what happens |
|---|---|---|---|---|---|---|
| 1 | 2:30:02 (31 s) | 67 | blunder | 6 7 7 7 7 6 | both | busted mid-escape: "I should have just laid low bro" |
| 2 | 3:21:54 (42 s) | 62 | blunder | 5 7 6 7 6 6 | content | the stolen car has a tracker; he spirals |
| 3 | 1:47:20 (36 s) | 61 | blunder | 5 6 7 7 5 6 | both | he shoots a gas can mid-chase: "that was sick" |
| 4 | 3:48:19 (27 s) | 59 | blunder | 6 6 6 6 6 5 | both | he walks into a store full of armed guards, panics, loses money |
| 5 | 0:09:58 (27 s) | 59 | npc_absurd | 5 6 6 7 5 6 | content | the necklace-collateral NPC ("very reasonable"); **= old clip 24** |
| 6 | 1:14:40 (22 s) | 58 | chaos | 5 6 7 6 5 5 | both | a fight flips: "you saw that? hell yeah" |
| 7 | 2:53:56 (35 s) | 55 | chaos | 5 5 6 6 6 5 | content | the police radio has no eyes on him as he escapes: "we out" |
| 8 | 2:34:38 (26 s) | 54 | chaos | 5 6 5 5 6 6 | signals | he sneaks up on a crowd and knocks people out |
| 9 | 2:51:43 (37 s) | 51 | chaos | 4 5 6 6 4 5 | both | a heist grab turns into a panicked escape |
| 10 | 1:12:03 (19 s) | 51 | chaos | 5 5 6 5 4 5 | signals | "oh my god, she got a knife!" |

**Vision coverage:** all 10 final picks were checked. 23 candidates and 226 images in total, $0.0009 per candidate.

**Old clips 24-31:**

- Clip 24 is #5 (the same scene).
- Clips 25, 26, 28 and 30 now score 42-50: below the top 10.
- Clip 27 scores 38, under min_score.
- Clips 29 and 31 were not proposed by either finder.

**Clip length (target 15-30 s):**

| set | median | p90 | over 30 s |
|---|---|---|---|
| top 10 | 28.8 s | 36.5 s | 50% |
| all 74 kept | 21.9 s | 36.5 s | 28% |

The first run was 6 of 10 at 40-45 s. Tightening the end to payoff + 1.5 s and starting long clips at the first beat
helped, but half the top 10 still run over 30 s.

**Score distribution:** 150 candidates were scored (26 content, 83 signals, 41 both).

| archetype | n | kept | mean | max |
|---|---|---|---|---|
| chaos | 28 | 20 | 48 | 77 |
| blunder | 21 | 16 | 47 | 67 |
| bit | 22 | 14 | 45 | 61 |
| glitch | 7 | 5 | 44 | 50 |
| npc_absurd | 12 | 7 | 44 | 59 |
| other | 54 | 11 | 35 | 61 |
| chat_interplay | 4 | 0 | 34 | 38 |
| discovery | 2 | 1 | 40 | 49 |

- Dropped by gate: 38 content (no clear trigger or payoff), 18 min_score, 14 skip rules, 5 window, 1 brief.
- `min_score` is still 40.

**Two problems you should know about:**

1. **The best moment of the VOD was dropped.** The tiger rant (raw score 77) failed the `window` gate. Haiku set
   its payoff 69 s after its first beat, and the rule drops a moment that can't fit 45 s rather than cutting it. The
   first run kept a 43 s version of the same moment at #1. I did not change this after the run, so the report
   matches the code. The fix is your call: either cap such a moment at 45 s from its first beat and end it at a
   pause, or keep dropping it.
2. **The peak-disk measurement is unreliable on the Mac.** Free space read through the bind mount rose during the
   download (APFS purgeable space). The real peak is about 2x the file size, roughly 25 GB, because yt-dlp writes a
   second, remuxed copy; the first run measured 26 GB.

**Spend and wall time (4.77 h VOD):**

| stage | wall time | USD |
|---|---|---|
| download (12.30 GB) | 9.0 min | - |
| transcribe (10 Groq chunks) | 3.7 min | 0.196 |
| signals | 5.8 min | - |
| content pass (29 calls) | 7.6 min (content pass, scoring and vision together) | 0.026 |
| scoring (150 candidates) | (included above) | 0.034 |
| vision | (included above) | 0.021 |
| **total** | **26 min** | **$0.276** |

All 152 cost rows were appended to the live ledger.

## Contract and config changes (CLAUDE.md: noted explicitly)

- **`schemas.py`:**
  - `MomentScore`: `reaction` / `event` / `setup` / `absurdity` are replaced by `scores: dict[str, int]`, and
    `found_by`, `content_confidence` and `vision` are added. v1 rows still read back: their four scores land in
    `scores`.
  - `CampaignIn` gains `auto_propose_vods` and `recent_vod_days`. A validator requires `allow_stream_download`,
    `stream_channels` and `recent_vod_days`.
- **Migration 0005** only adds columns and the `vod_proposals` table. The v1 score columns stay.
- **`config.py`:**
  - `per_source_cap_usd` (1.50) and `min_free_gb` (15) are new, with defaults, so your local `clipforge.yaml` needs
    no edit.
  - New optional secrets `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET`.
- **Campaigns:**
  - Boeshi: `max_source_minutes` removed, `auto_propose_vods: true`, `recent_vod_days: 7`.
  - Dylan Page and AsianJeff: `moment_profile: gta6-streamer`.

## What you need to do after merging

1. Add `TWITCH_CLIENT_ID` and `TWITCH_CLIENT_SECRET` (a Twitch dev app, client-credentials) to `.env`. I could not
   touch `.env.example`: the repo hook blocks any `.env*` path. Without them the proposal job just logs and skips.
2. Re-run `cf campaign add campaigns/<file>.json` for Boeshi, Dylan Page and AsianJeff. The DB still holds Boeshi's
   `max_source_minutes=60` until you do.
3. **Confirm `recent_vod_days: 7`.** The brief says only "recent VODs", so 7 is my guess; Add re-checks it at tap
   time.
4. Rebuild the image and restart (`make up`). I did not do this.
5. Last night's nightly run, purge and backup were missed: the Mac slept from 01:58 to 13:20 UTC. Run
   `cf run --once` or wait for tonight, and keep the Mac awake at night.
6. Decide the tiger-rant rule (section 10).

## Verified

- **Tests:** `make lint`, `make typecheck` and `uv run pytest -q` all pass. The whole default suite also passes
  **inside the image** (bookworm ffmpeg 5.1), including the new real-ffmpeg tests for JPEG keyframes, the facecam crop
  and silencedetect.
- **Pre-existing failure, not from this branch:** the `slow` faster-whisper test fails in the image. `av.open()`
  rejects `metadata_errors`, a version mismatch between av and faster-whisper; this branch doesn't touch that code.
- **Compliance review of the watchdog and window diff:** PASS, `tests/compliance` 252 passed. Its three notes are
  fixed: a retried download re-runs the channel, live and disk checks; a watchdog error in Phase B fails the
  candidate, not the night; a step outside any stage has a ceiling.
- **First compliance review:** the compliance-reviewer subagent first returned FAIL, with two blockers:
  - shadow-rank on a DB copy didn't check the live monthly cap
  - `recent_vod_days` was not re-checked at tap time

  Both are fixed, with tests, and the re-review is a PASS. Its should-fix items are done too: the prompt lines on
  sexual/graphic content, a non-owner button test, the copy-time source cap, the pause flag and the mid-run cap.
  `tests/compliance`: 252 passed.
- **Data sent out:** keyframes of the licensed footage now go to Anthropic, for that campaign only, like transcripts
  to Groq.
- **Small deviation from the plan:** footage is purged at the end of each nightly run, not on every review or publish
  change. A source rejected in the afternoon loses its file that night.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
