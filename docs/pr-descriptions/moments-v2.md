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
- End at most 1.5 s after the payoff.
- Aim for 15-30 s, with 12 s minimum and 45 s hard cap.
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

**Full-VOD numbers (source 2, 4.77 h):**

| | |
|---|---|
| Download | 12.30 GB in 13.4 min |
| Peak disk | about 26 GB (yt-dlp remuxes to a second temp file) |
| Transcribe | 3.8 min, 10 Groq chunks, $0.20 |
| Signals | 5.7 min |
| Content pass, scoring and vision | about 4.4 min |
| Total spend | **$0.24** (first run) |

`RunReport` now prints one line per source with file size, free disk, stage times and spend.

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

## 10. Source 2 with the new logic

This is the full report from `cf source shadow-rank 2 --full --compare-clips 24-31`:
[moments-v2-source2-shadow.md](moments-v2-source2-shadow.md). It ran in a one-off container on a copy of the
database, and wrote no candidates.

**Caveat:** this run came before the last commit. In it, only the 12 shortlisted candidates were vision-checked (some
picks show "-" under frames), and the candidate pool was capped at 40 (59 of the 99 content proposals were never
scored). A second run with the final logic failed (see below).

**Top 10, with the six scores in order hook arc arousal sendability clarity density:**

| # | window | score | archetype | scores | found by | what happens |
|---|---|---|---|---|---|---|
| 1 | 4:15:58 (43 s) | 61 | chaos | 6 5 7 7 5 6 | both | a tiger attacks mid-theft, he dies and rants "a tiger killed me" |
| 2 | 4:16:51 (30 s) | 60 | other | 6 6 7 6 4 6 | content | a 1 HP clutch after the tiger; he repeats "I did it" in disbelief |
| 3 | 1:47:16 (40 s) | 58 | chaos | 6 6 6 6 5 5 | both | a stolen ambulance, birds dive, he sets it on fire: "that was sick" |
| 4 | 1:30:11 (41 s) | 57 | chaos | 6 5 6 6 6 5 | content | a cop attacks him, he gets busted: "why is he attacking me" |
| 5 | 2:52:06 (41 s) | 55 | chaos | 5 6 6 5 5 6 | both | a heist grab, then a sprint out on one heart: "we escaped" |
| 6 | 1:39:53 (45 s) | 54 | npc_absurd | 5 5 6 6 5 5 | content | he argues with a cop over trespassing and apologises to the NPC |
| 7 | 1:04:07 (27 s) | 53 | glitch | 6 5 6 5 4 5 | both | hit by a train, then a chasing car crashes after he jukes it |
| 8 | 2:49:08 (31 s) | 53 | blunder | 6 5 5 6 4 5 | content | he runs over a gnome; the radio reports a hit-and-run: "that is not us" |
| 9 | 1:32:29 (30 s) | 52 | chaos | 5 5 6 5 5 5 | signals | he keeps getting picked off: "I'm getting pissed" |
| 10 | 1:04:43 (18 s) | 52 | chaos | 5 5 6 5 5 5 | signals | a car-dodging chase ends in a cartoon crash: "what the hell" |

**Clips picked before (24-31).** None of them is in the new top 10.

- All eight come from the first hour: 0:08-0:58. That was all v1 ever saw, because of `max_source_minutes: 60`.
- The new top 10 spans 1:04-4:17. Eight of the ten were found by the content pass alone or together with signals; only two came from signals alone.
- The best window in the first hour under v2 is the NPC "collateral / very fair trade" bit at 0:09:57, with raw score
  49. It is the same scene as old clip 24 ("He holds your necklace until you pay up"), and it ranked below the top
  10.

**Score distribution, every scored candidate:**

| archetype | n | kept | mean | max |
|---|---|---|---|---|
| chaos | 10 | 8 | 50 | 61 |
| bit | 4 | 4 | 48 | 51 |
| npc_absurd | 4 | 3 | 47 | 54 |
| glitch | 3 | 2 | 46 | 53 |
| blunder | 6 | 5 | 45 | 53 |
| other | 13 | 6 | 39 | 60 |

- Dropped: 4 by skip rules, 6 by the content gate (no clear trigger or payoff), 2 under min_score 40.
- The new scale runs lower than v1: the top score is 61, and most candidates fall between 45 and 55. **I left
  `min_score` at 40.** At 40, 28 of 40 candidates were kept. At 50, at least 15 would be (the 10 picks plus the 5 discarded windows scoring 50-51).

**The 10 best discarded windows** have raw scores 47-51, and are listed with reasons in the report. Nine were simply
below the top 10:

- a rent-collecting NPC repeating "accountability"
- a mock-apologetic robbery monologue
- a waypoint distance jumping mid-drive
- a "Fudge! Fudge!" stairway fail
- a self-own in a gun shop
- and others

The one gate drop was a SWAT standoff (`content: no clear payoff`).

**Observed issue:** windows tend toward the 45 s cap (6 of the top 10 are 40-45 s), despite the 15-30 s target.
The arc score covers length, but nothing enforces it. It's worth watching in the first reviews.

**Spend:**

| stage | USD |
|---|---|
| Groq transcription (4.77 h) | 0.196 |
| content pass, 29 calls | 0.026 |
| scoring | 0.009 |
| vision, 12 candidates and 119 images | 0.011 ($0.0009 per candidate) |
| **total** | **$0.24** |

All 102 cost rows were appended to the live ledger.

**Second run (final logic) failed.** It started 27 Nov 01:45 UTC. This time the Twitch download took hours, and then
audio extraction of that file hit the new 4 h ffmpeg timeout (13:20 UTC). Nothing was spent, and the scratch footage
was deleted. I don't know the cause: a slow download leaving a bad file is my best guess.

A real risk follows from it: a hung ffmpeg pass can now hold a nightly run for up to 4 h. I did not re-run it a third
time; say if you want it.

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

## Verified

- **Tests:** `make lint`, `make typecheck` and `uv run pytest -q` all pass. The whole default suite also passes
  **inside the image** (bookworm ffmpeg 5.1), including the new real-ffmpeg tests for JPEG keyframes, the facecam crop
  and silencedetect.
- **Pre-existing failure, not from this branch:** the `slow` faster-whisper test fails in the image. `av.open()`
  rejects `metadata_errors`, a version mismatch between av and faster-whisper; this branch doesn't touch that code.
- **Compliance review:** the compliance-reviewer subagent first returned FAIL, with two blockers:
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
