# feat/render — Phase 1 C: render, QA, assets

Turns a ranked candidate into a 1080×1920 vertical clip. The clip gets word-highlight captions, the hook overlay
for the first 2.5 s and the "Ad" label from 0 to 3 s. Render writes a `schemas.RenderManifest`, and QA gates the clip
with a `schemas.QAReport`. Render does not touch the database: Phase 2 creates the `clips` rows and runs the state
transitions.

## Tasks done

| Task | What | Main files |
| --- | --- | --- |
| C1 | Shot detection: PySceneDetect `ContentDetector` limited to the candidate range. A pure `normalize_shots` keeps the shots contiguous and merges any under 1 s. | `render/shots.py` |
| C2 | Pure reframe planner over face boxes produces `CropPlan`: largest face, median x-centre, width = h × 9/16, clamped. Two big faces far apart → `fit_blur`. Uses the MediaPipe Tasks FaceDetector adapter (lazy import, 5 fps sampling). | `render/reframe.py`, `render/face_detect.py` |
| C2b | `content_kind == "game"` → `fit_blur` for every shot, and detection is skipped. No faces, or faces in under 30% of the sampled frames → `fit_blur`. No detector available → `fit_blur`. | `render/reframe.py`, `render/clip.py` |
| C3 | Pure ASS writer and captions. Chunks are 2–4 words, ≤ 18 characters per line, and break at punctuation or a pause. One event per word highlights the active word. Captions sit in a 62–70% safe zone. All 3 presets are supported (outline / outline / box). | `render/ass.py`, `render/captions.py` |
| C4 | `ad_label_event` (0.0 → `render.ad_label_seconds`, always present) and the hook (top third, 0 → `hook_seconds`). OFL fonts are bundled, and each is checked by its PostScript name against the config `styles`. | `render/overlays.py`, `render/assets.py`, `assets/` |
| C5 | One ffmpeg run on one input: `trim` + crop/fit_blur per shot → `concat` → `ass` → `fps` → `yuv420p`. Encoding is `libx264 veryfast crf 21`, `aac 128k`, with `loudnorm=I=-14:TP=-1.5:LRA=11`. `render_candidate` orchestrates shots → plan → ASS → encode → manifest and refuses silent sources (PRD F17). | `render/encode.py`, `render/clip.py`, `qa/probe.py` |
| C6 | `qa_clip`/`run_qa` produce a `QAReport` (checks listed below). `preview_for_telegram` returns the clip, or a 480p proxy when the clip is over 50 MB. | `qa/checks.py`, `qa/preview.py` |
| C7 | `scripts/bench_render.py` prints seconds per output second. | `scripts/bench_render.py` |

**QA checks:**
- file SHA-256 matches the manifest
- duration is 10–90 s and matches the manifest (±0.5 s)
- 1080×1920, fps, h264, exactly one AAC stream
- `compliance.render_manifest_problems`
- campaign active
- per platform: `compliance.assert_platform_allowed` (political, account-enabled)
- per platform caption: present, starts with `#ad `, contains every campaign `required_tags`, within the limit,
  no URL on X

### Compliance: how invariants 3 and 10 are enforced

- **3, "Ad" label 0–≥3 s.**
  - The label is an ASS event built from config. Config validation keeps it at ≥ 3 s, and it is always emitted,
    even when the hook is empty.
  - The manifest's `has_ad_label` is true only when the exact `AdLabel` Dialogue line and its `Style:` line are in
    the ASS file that libass burns. A blank `ad_label_text` is refused before anything is written.
  - `render_candidate` calls `compliance.assert_render_manifest` before it returns, and QA checks the manifest again.
- **10, no added music.**
  - The ffmpeg command has exactly one `-i` (the source).
  - Audio goes through `-map 0:a:0` with `-af` and is never routed through a filtergraph label.
  - `maps_only_source_audio(args)` inspects the final argument list. Any of these makes it false:
    - more than one input
    - a map other than `[v]`, `0:v…` or `0:a…`, including a bare `-map 0`
    - an audio pad such as `[0:a]` in any filter
    - `movie`/`amovie`/`amix`/`amerge`/`join`/`sine`/`anullsrc`/`aevalsrc`
    - filter scripts or `-stream_loop`

    File paths inside `filename=`/`fontsdir=` are ignored, so a folder named `movie-night` is not refused.
  - The manifest's `audio_source_only` is that result, not a constant. A violation stops the render before ffmpeg
    runs (tested).
  - QA also requires exactly one audio stream in the output.
- **10, duration.** Lengths outside 10–90 s are refused before encoding (`compliance.assert_duration_bounds` and
  config `pipeline.min/max_clip_s`). QA checks the probed duration again.

## Tests added

- `tests/render/`: shots (including hypothesis), reframe, ass, captions (including hypothesis), overlays + assets,
  encode, clip (orchestrator with injected fakes), bench.
- `tests/qa/`: probe, checks, preview.

Coverage on this branch: `ass`, `overlays`, `reframe`, `shots`, `qa/checks`, `qa/preview` at 100%; `captions` 98%;
`encode` 91%; `render/` + `qa/` 88% overall.

**Skipped on the Mac, run in CI and Docker (not yet run anywhere):** this Mac has no ffmpeg, Homebrew or Docker,
and mediapipe is Linux-only. These tests run for the first time in CI on this push:
- `test_real_render_of_fixture[game|talk]`: a real 12 s render with libass and the bundled fonts, checked with ffprobe.
- `test_ad_label_pixels_visible_then_gone`: compares the clip with a baseline encode of the same window that has no
  ASS file. The label box must differ at 0.5 s and 2.5 s and match at 4 s. The thresholds (> 15 and < 4 mean grey
  levels) are first guesses. If CI fails here, tune the thresholds or the box and keep the invariant as it is.
- `test_real_proxy_from_fixture`, `test_probe_fixture`, `test_bench_smoke`.
- `test_faceless_fixture_defaults_to_fit_blur`: real MediaPipe on the face-less fixture.

Check the first CI run of this branch before merging. The filtergraph strings are unit-tested but have not been
run through a real ffmpeg yet.

**Shared test files touched:**
- `tests/compliance/test_inv03_ad_label.py`: the strict-xfail marker on
  `test_overlay_ad_label_spans_first_three_seconds` was removed.
- `tests/compliance/test_inv10_duration_audio.py`: the strict-xfail markers on `test_encoder_maps_only_source_audio`
  and `test_qa_rejects_out_of_bounds_duration` were removed.
- In both files `xfail_until` was then unused, so it was dropped from the import line (ruff F401).
- Nothing else in `tests/compliance/` changed. `tests/test_cli.py` was not touched.

## External facts verified

| Fact | Source |
| --- | --- |
| PySceneDetect 0.7.3 API: `open_video`, `VideoStream.seek(seconds)`, `SceneManager.add_detector` / `detect_scenes(video, end_time=…)` / `get_scene_list()` → `FrameTimecode.seconds`, `ContentDetector(threshold=27.0, min_scene_len=…)` | Installed package source (`.venv/.../scenedetect/scene_manager.py`, `common.py`, `detectors/content_detector.py`) |
| MediaPipe FaceDetector Python API: `mp.tasks.BaseOptions(model_asset_path=…)`, `mp.tasks.vision.FaceDetectorOptions(running_mode=RunningMode.IMAGE, min_detection_confidence=…)`, `FaceDetector.create_from_options`, `detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))` | https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector/python (redirects to developers.google.com) |
| Result fields `detections[i].bounding_box.origin_x/origin_y/width/height` (pixels), `categories[0].score` | https://github.com/google-ai-edge/mediapipe `mediapipe/tasks/python/components/containers/{bounding_box,detections,category}.py` |
| BlazeFace short-range model URL; Apache-2.0; 229,746 bytes; SHA-256 `b4578f35…0152f`. The downloaded file matches. | https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector (Models), https://huggingface.co/litert-community/MediaPipe-FaceDetector-BlazeFace-Short-Range |
| Telegram: multipart uploads are limited to "50 MB for other files". The proxy threshold is 50,000,000 bytes. The `sendVideo` section's own sentence was not read verbatim (the page is too long for the fetcher), so the limit comes from the "Sending files" section. | https://core.telegram.org/bots/api#sending-files |
| Fonts are SIL OFL 1.1, static TTFs (libass uses only the default instance of a variable font). Montserrat ExtraBold comes from JulietaUla/Montserrat; Inter Bold and Black from the rsms/inter v4.1 release. SHA-256s are in `assets/README.md`. | https://github.com/JulietaUla/Montserrat, https://github.com/rsms/inter/releases/tag/v4.1 |

No HTTP calls were added to `src/`, so nothing needed respx mocks. The assets were downloaded once and committed.

## Contract requests

See `docs/contract-requests/render.md`:
1. **Required:** the Dockerfile needs `COPY assets ./assets`. Without it the image has no fonts or face model, and
   render refuses to run.
2. Optional: `CLIPFORGE_ASSETS_DIR` in `config.RuntimeEnv`.
3. The table of `ClipRequest` inputs that the pipeline supplies (no `schemas.py` change).

## Design notes for review

- **Second face counts only if it's big enough.** A second face only splits the frame (→ `fit_blur`) when its area is
  ≥ 25% of the largest. This keeps a small background face or poster from forcing `fit_blur` on a talking-head shot.
  It refines the ARCHITECTURE §4.4 rule.
- **Crop width.** The crop width is rounded down to an even number (1080 p → 606 px).
- **`build_ffmpeg_args` signature.** It takes the manifest first, as `test_inv10` expects. The other inputs are
  keyword arguments with safe defaults; with no crop plan it renders one `fit_blur` shot.
- **Unsafe file paths are refused.** Paths that ffmpeg filter options would misread (`:`, `'`, `,`, `[`, `]`, `;`,
  `=`, `\`) are refused rather than escaped. Every path render writes is under `data/`.

## Left for Phase 2

- `pipeline.py`:
  - create the `clips` row
  - call `render_candidate`, then `qa_clip`
  - store `qa_report_json`
  - transition to RENDERED → PENDING_REVIEW / QA_FAILED with `events` rows
  - pass `content_kind`, transcript words and the hook
- **New hook:** overlays are burned in, so a new hook means calling `render_candidate` again (a full encode, with
  the shots and the crop plan computed again). If that's too slow, add an optional precomputed `CropPlan` to
  `ClipRequest`.
- **Shorter:** trim 20% using word timestamps, then call `render_candidate` again with the new range.
- **Bot previews:** `review` sends `preview_for_telegram(clip_path)`.
- **Phase 3:** `scripts/bench_render.py` on real footage (P3.1) to tune `crf` and threads, and to check the
  quarter-size blur cost.

## Compliance review

I ran the `compliance-reviewer` subagent twice on `git diff main...HEAD`.

**First run: FAIL, 4 required fixes, all applied in `3f0200e`.**
1. `has_ad_label` was a substring test on the ASS text. It could be true with a blank label (`ad_label_text: "   "`
   passes config validation). It now checks the exact Dialogue line, and blank text is refused. Config should
   also reject blank text at load time; see contract request 2b.
2. `audio_source_only` was hard-coded to true after the check. It now records the computed result, and the refusal
   path is tested.
3. There was no proof from real pixels. Added the CI pixel test above.
4. QA did not check `required_tags`. It does now.

It also recommended hardening the audio-map check, which is done.

**Second run: all four fixes confirmed, 9 invariants untouched, none weakened.**
- 2 and 9 (QA side) and 10 are stronger. 3 is preserved.
- The verdict stays FAIL until CI runs, because the pixel proof for invariant 3 has never executed (no ffmpeg on the
  Mac). **Merge condition:** the CI run of this branch must show `test_ad_label_pixels_visible_then_gone` and
  `test_real_render_of_fixture[game]` / `[talk]` **passed, not skipped**.
- After that review I made the pixel test sturdier (baseline comparison in place of a bright-pixel count) and kept
  file paths out of the audio check.

**This branch tightens invariants 2, 3 and 10 and weakens none.** `compliance.py`, `schemas.py` and `config.py` are
unchanged.

Non-blocking notes for Phase 2:
- `qa.checks.has_tag` copies the tag regex from `compliance._has_tag`. Expose one public `compliance.has_tag`.
- Add the YouTube title limit (100) to QA before YouTube is enabled.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
