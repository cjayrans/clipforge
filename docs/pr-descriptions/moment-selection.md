# Moment selection: pick the GTA 6 moment types that get views (`feat/moment-selection` → `main`)

Written for Carson.

The old ranker scored 20–75 s transcript windows by speech density. That suits talk content, but it picks the
"boring" game footage: walking, driving, menus. This PR adds a **moment ranker** that follows
`docs/research/clipping-gta6-moment-types.md` (copied into the repo). Views come from a **visible reaction to
something going wrong, absurd or escalating**. So a candidate exists only where a reaction follows a trigger. Haiku
sorts each candidate into an archetype and scores it on four axes.

A campaign opts in with `"moment_profile": "gta6-streamer"` in its JSON. **Boeshi is opted in.** Dylan Page and
AsianJeff are not (one line each to opt in). Talk campaigns keep the old ranker. All GTA-specific knowledge lives in
`config/moment_profiles/gta6-streamer.yaml`; a test fails if `rank/`, `track/moments.py` or the ranker prompt names a
game, streamer or publisher.

## How a source is ranked now (moment campaigns)

1. **Signals over the whole VOD**, all cheap:
   - per-second loudness (one ffmpeg `astats` pass)
   - frame-difference motion on keyframes only (one ffmpeg `signalstats` pass)
   - the Groq transcript: stressed exclamations ("no no no", "what?", "oh my god"), laughter tokens, cue phrases
     ("look at this", "wait"), words to chat
   - silence beats: a speech gap followed by a reaction

   Loudness and motion are z-scores against a trailing 60 s median/MAD baseline.
2. **Hard gates before ranking**
   - `trigger_reaction`: no candidate unless a reaction follows a trigger within 3 s.
   - `dead_footage`: reject when the loudness spread is under 3 dB and there is no motion spike.
3. **Window rules**: start 2–5 s before the trigger (snapped to a word edge). End 1.5 s after the payoff (a chain of
   reactions less than 4 s apart). 12 s minimum, 12–35 s target, 45 s hard cap. Overlaps are removed and the top 40
   by pre-score go to Haiku.
4. **Haiku** (`score_moments`, forced strict tool) returns per candidate:
   - an archetype: blunder, shock, NPC weirdness, glitch, chaos, chat interplay or other
   - reaction / event / setup clarity / absurdity, each 0–10
   - a proposed start/end, skip flags, review flags and a one-line reason
5. **Gates after scoring**
   - `brief`: hateful, harassing or off-brief content is dropped.
   - `skip_rules` (Rockstar policy):
     - **rejected**: mostly cutscene, ending or story reveal, an in-game show or radio bit on its own
     - **penalised ×0.6**: partly cutscene
   - `min_score`: under 40 is dropped.

   Weighted score = 10 × (0.35 R + 0.25 E + 0.25 S + 0.15 A), which becomes `Candidate.score`. The model's window is
   used only if it obeys the window rules.
6. **Sexual or graphic content is flagged, not rejected.** The clip is scored on the blunder and the reaction. The
   Telegram preview gets `CHECK BEFORE APPROVING: flagged sexual`. The profile's sensitive words in the transcript
   add a flag as well. Every clip still needs your approval as before. A penalising skip rule (partly cutscene)
   shows as `Skip rules (score lowered): cutscene_part`, because the ranker cannot see the video.
7. **Copy**: `copy_style_notes` still reach both the ranker brief and the copywriter. The copywriter now also gets
   the ranker's one-line note on what happens (clip_copy v3, `$moment_note`). Game audio alone often says little.
   "New hook" re-renders get the same note.

## Calibration

- Each candidate stores its archetype, the four scores, `weights_version`, the profile name, every gate result
  (pass/fail/not evaluated, with details), skip flags and review flags. This uses migration `0003`, which adds
  nullable columns only.
- `posts.views_24h` / `views_72h` are nullable.
- `cf report moments`:
  - Fills due 24 h / 72 h values from the view snapshot nearest the mark (±6 h). It never overwrites a value.
  - Then prints, per archetype: posts, average score, and median/mean 24 h and 72 h views, plus 72 h views per
    platform.
  - Without views yet, it says so.

## Decisions I made (you said not to ask)

1. **Opt-in per campaign, by profile file.** `moment_profile` names `config/moment_profiles/<name>.yaml`. The file is
   not inline in the campaign JSON, so Dylan Page and AsianJeff can share it. `config/` is already mounted into the
   containers. `cf campaign add` refuses an unknown or invalid profile. A missing profile at rank time raises an
   alert and leaves the source TRANSCRIBED for the next night.
2. **Face-cam expression change is not implemented.** MediaPipe is a dependency, but the image ships only the
   BlazeFace *detector*, which returns boxes and no expressions. Expressions need the FaceLandmarker model (blend
   shapes): a new ~4 MB asset and a rebuild. HUD detection and chat velocity were skipped as you allowed (no chat
   data in campaign footage). Pitch was not in your list, so I left it out.
3. **Motion uses keyframes only by default** (`-skip_frame nokey`, about one every 2 s on Twitch). In the image it
   took 1.5 s for 2 min of 1080p60. Full decode would take minutes per hour of VOD.
   `motion_keyframes_only: false` samples at `motion_sample_fps` instead.
4. **The cutscene, story-reveal and in-game-show rule is judged by Haiku and applied as a gate right after
   scoring.** It needs to know what is on screen, which no cheap signal gives. Before ranking, the
   trigger+reaction gate already drops cutscenes nobody reacts to.
5. **Failed media signals degrade instead of failing.** If ffmpeg fails or the file is missing, ranking runs on
   transcript signals. The `dead_footage` gate records "not evaluated", and the run summary lists a problem line.
6. **Loudness spikes need 6 dB over the trailing median** (z 2 with a 3 dB sigma floor). On near-constant audio the
   MAD collapses, and a lower floor turned ±1.5 dB wiggle into spikes.
7. **Views live on `posts`** (one row per clip × platform), not on clips. TikTok and Instagram numbers differ, and
   pay differs per platform (PRD F15). The report shows per-archetype totals and the per-platform split.
8. **`Candidate.score` stays 0–100**, so render ordering, the backlog and the preview "Score" keep working.
   `standalone` = setup ≥ 5.
9. **Reaction words are matched only when stressed**: ending in ! or ?, repeated, or in an utterance of 3 words or
   fewer. A plain "what do you want" is not a reaction.
10. **Nothing already ranked is touched.** Only TRANSCRIBED sources are ranked, as before. The migration adds
    nullable columns, and a test upgrades a 0002 database holding an approved clip and checks every old value.
11. **Weights and thresholds are the research doc's guesses** (`weights_version: gta6-streamer-v1`). Re-fit after
    20–30 posted clips and bump the version.

## Contract change (schemas.py, flagged on purpose)

- `CampaignIn.moment_profile` (optional; pattern `^[a-z0-9][a-z0-9_-]{0,63}$`)
- new `GateResult` and `MomentScore`
- `Candidate.moment` (optional)

Older JSON and rows load unchanged. `Window` (20–75 s) is untouched: moment windows use their own dataclass.

## Not done / follow-ups

- No setter for views besides snapshots: `cf import-reconcile` and the YouTube poller write snapshots, and
  `cf report moments` turns them into 24 h / 72 h values.
- Face expression signal (needs the FaceLandmarker asset).
- No pre-rank letterbox/cutscene detector.
- The eval harness (`evals/`) still targets the window ranker.
- None of this runs in the containers until the image is rebuilt (`src/` and `prompts/` are copied into the image).
  I did not rebuild or restart anything. The Dockerfile's build check now also loads `score_moments`.

## Verification

- `make lint`, `mypy --strict` on schemas/compliance, and the full suite pass locally (1341 passed, 13 skipped). New
  tests cover the profile, signals, generation and gates, the ranker (strict schema, parsing, gates, window rules,
  end to end with the mocked Anthropic API), the ffmpeg passes, the pipeline wiring, migration 0003, the report and
  the preview.
- The real-ffmpeg test (synthetic file: loud burst and a scene cut at 20 s) is skipped on the Mac, which has no
  ffmpeg. It passed inside `clipforge:latest` (Debian ffmpeg 5.1) via a throwaway `docker run --rm` with the branch's
  `src/` mounted read-only. CI runs it on its newer ffmpeg.
- Compliance-reviewer subagent: **PASS, nothing blocking.** Every invariant is preserved or untouched. The compliance
  tests pass on the branch and on the branch merged with main. I acted on three of its non-blocking points: the
  preview shows skip flags, a pipeline test takes a flagged clip to PENDING_REVIEW with the manual-check line, and a
  spend-cap test covers the moment ranker (no request is sent). The ffmpeg stderr tail in a local log line is not
  redacted (info only: the arguments are a local path, and the run-summary copy is redacted).
- No live API calls. `PUBLISH_MODE` and `.env` untouched.
