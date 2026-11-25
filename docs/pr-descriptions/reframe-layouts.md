## Layouts for game footage: stacked facecam, AutoFlip-style gameplay crop, fit as the fallback

Game clips no longer shrink the whole 16:9 stream into the middle of the 9:16 frame. Each clip now picks one of
three layouts:

- **stacked**: the streamer's facecam scaled into a 1080×640 panel at the top, with a gameplay crop (1080×1280) below.
- **crop**: a full-height 9:16 gameplay crop.
- **fit**: the old blurred-background fit, now only a fallback.

With `layout: auto` (the default) a clip tries stacked first, then crop, then fit. It moves on when a layout's
prerequisites fail or when the hook, "Ad" label, captions and campaign overlay cannot all be placed clear of each
other, of the platform UI and of the face. Every clip records the layout it used and why it skipped the others. The
Telegram preview shows the layout and the render time.

`cf clip relayout` re-renders clips that are still pending review through the normal re-render path. It keeps each
clip's window, hook and captions, and the bot resends the preview.

### Decisions

Confirmed by Carson during planning:
1. **Stacked split 640 / 1280.** Facecam 1080×640 on top, gameplay 1080×1280 below. The gameplay crop keeps about
   47 % of a 16:9 frame's width, and a banner overlay at y ≈ 31 % sits on the seam.
2. **Stacked "Ad" label at the facecam panel's top-left corner** (x 48, just under the 150 px top UI zone), on its
   75 % dark box, 0 to ≥ 3 s, checked clear of the face. If the face is in that corner, the label goes under the hook.
   This is **label layout 3** (COMPLIANCE.md invariant 3 updated). The old rule ("under the hook") would not fit
   with a 3-line hook: the hook has to move below the overlay so it stays off the face, and the label under it would
   run into the caption band.
3. **Facecam check by eye before re-rendering.** `cf source facecam <id> --debug-image` writes a sample frame with
   the boxes drawn and a stacked mock.
4. **A missing `layout` means `auto`** for every game campaign. Talk campaigns keep the face-crop planner, recorded
   as crop or fit.

Made while building it:
5. **Facecam detection uses tiles.** The bundled MediaPipe BlazeFace short-range model looks at a 128×128 input, so a
   ~100 px facecam face in a 1080p frame would vanish on the full frame. Detection runs on overlapping tiles
   0.4 × frame height in size, then NMS. 40 frames are sampled across the source (first and last 5 % skipped).
   Faces taller than 35 % of the frame (full-cam scenes) or with a score under 0.5 are dropped. The facecam is the
   cluster of face centres seen in ≥ 40 % of frames; in-game faces move and never cluster. Its box is the median
   face, padded to the panel aspect (2.4 × face height, face 45 % down) and kept inside the frame.
6. **Stored once per source** on `sources.facecam_json`, "none" included, with an events row. A detection that could
   not run (no mediapipe, unreadable media) stores nothing and runs again next time. A new `DETECTOR_VERSION`
   re-detects.
7. **Manual override `facecam_box`** is in percent of the source frame, so it holds for any VOD resolution. It is
   trimmed to the panel aspect around its centre and skips detection.
8. **Per-clip face check.** Before stacked is used, the facecam box is cropped from the clip's own range at 2 fps and
   a face must be in ≥ 60 % of those frames. This covers the streamer switching scenes or leaving the cam. QA then
   checks the **rendered** file's top panel (6 frames, ≥ 50 % with a face). With no detector available that QA check
   fails; it is never skipped.
9. **Gameplay camera (AutoFlip-style).** One ffmpeg motion pass per clip (4 fps, 192 px grey, facecam masked).
   - Per shot (PySceneDetect shots, as before), stationary centre is the default.
   - Concentrated motion that holds still off-centre gives a stationary crop at its median.
   - Tracking happens only when concentrated motion is outside the centre window in ≥ 30 % of the shot and actually
     moves. The path is median-filtered, has a dead zone, is rate-limited at **15 % of the source width per second**
     (288 px/s on 1080p; the planner aims at 90 % of that), smoothed and clamped.
   - Global camera motion spreads over the whole frame, so it never counts as concentrated and never moves the crop.
   - The encoder gets the path as a piecewise-linear `crop` x expression in `t`, a balanced `if` tree. No new
     dependencies: ffmpeg, numpy, OpenCV and MediaPipe are already in the image.
10. **Crop vs fit.** In the crop layout, a shot whose motion splits into two far-apart spots gets the fit for that
    shot. If every shot does, the clip is recorded as fit. Stacked always crops, since it keeps 47 % of the width.
11. **Placement per layout.** In crop and fit, the hook, label and overlay go exactly where they went before (layout 2
    positions). In stacked, the hook moves to the first spot clear of the face, the overlay's visible pixels, the UI
    and the captions: its default spot, then just under the overlay, then just under the facecam panel. With the
    Boeshi overlay the hook lands at about y 721–1079, the worst-case 3-line box, clear of the captions (≥ 1131).
    Nothing is ever shrunk. A conflict moves auto to the next layout; a layout set explicitly is refused with the
    reason.
12. **New manifest geometry** (label layout 3): hook box, caption band, face and facecam panel (stacked), crop plan with
    keyframes, pan limit, stage timings. `compliance.layout_geometry_problems` fails a clip in these cases:
    - the hook, label or overlay covers the face;
    - the hook or captions cover the overlay (it stays unobstructed);
    - the captions or hook reach the top or bottom UI zone;
    - geometry is missing.

    - the "Ad" label overlaps the hook box or the caption band.

    Layout 1 and 2 manifests keep their old rules. New QA checks: `crop_bounds`, `pan_speed`, `facecam_face`.
13. **`cf clip relayout`** only accepts `pending_review`. Approved, scheduled, published, rejected and QA-failed clips
    are refused before anything renders. It reuses the New hook / Shorter machinery:
    - the clip moves to RENDERED while it renders, then gets a new file and a fresh `rendered_at`, so old approvals
      go stale;
    - the bot sends the new preview because `rendered_at` changed;
    - a failure keeps the old file and returns the clip to review.

    `--dry-run` lists what would happen. `--wait-previews N` waits until the bot has sent the new previews and
    reports how many.
14. **`cf source facecam <id> [--redetect] [--debug-image PATH]`** prints the stored or detected box and writes
    `PATH` and `<stem>_stacked.jpg`.
15. **Campaign validation**: `layout: stacked` checks a campaign overlay against the stacked arrangement. Any other
    layout checks it against the default arrangement, which auto, crop and fit fall back to.
16. **Generic:** nothing in the code knows about any campaign. Boeshi only gets `"layout": "auto"` in its JSON.
17. **CLAUDE.md** line on game footage updated from "default to the blurred-background fit" to the layout rule (it
    was no longer true).

### Compliance review
The compliance-reviewer subagent found no blocking issues. All four of its non-blocking findings were acted on:
- QA now also checks that the label overlaps neither the hook nor the caption band.
- A new invariant 10 test covers stacked + tracking + overlay filtergraphs, plus injected audio pads and filters.
- The COMPLIANCE.md row 3 wording now matches the code.
- CAMPAIGNS.md says the debug stills are licensed footage: keep them local and delete them after checking.

### Contract change (`schemas.py`)
`CampaignIn.layout`, `CampaignIn.facecam_box` (`FacecamBoxPct`, talk + stacked refused); `CropMode.GAME_CROP` /
`STACKED`; `PxBox`, `CropKeyframe`; `ShotCrop.y/camera/keyframes/facecam`; `RenderManifest.layout/layout_reason/
hook_rect/caption_rect/face_rect/facecam_rect/crop_plan/max_pan_px_s/timings`. All new fields have defaults, so
existing manifests and campaign files stay valid.

### Database
Migration 0004 only adds columns: `campaigns.layout` (default `auto`) and `campaigns.facecam_box`,
`sources.facecam_json`, `clips.layout` and `clips.render_s`. Existing rows keep every value.

### Compliance
Invariant 3: label layout 3 as above. No rule is relaxed: the label keeps 0 to ≥ 3 s, the dark box and every UI
zone; overlays still never cover the label, hook or captions. The layout-3 checks add rules: face clearance,
overlay unobstructed, captions and hook out of the top and bottom UI. Invariant 10: the stacked and tracking
filtergraphs pass `maps_only_source_audio`; they have no input pads besides `[0:v]`/`[1:v]` and no audio filters.
Nothing here touches publishing, approvals or `PUBLISH_MODE`.

### Render time (bench in the image, `--cpus 6`, synthetic 1080p fixture, 3 × 30 s)
fit 0.53, crop 0.57, stacked 0.50 seconds per output second, about 15 s per 30 s clip. The motion pass adds about
1.3 s per clip. The per-clip facecam check is not in these numbers (the fixture has no face); real per-clip times
are in the relayout report.

### Tests
New:
- `tests/render/test_facecam.py`: clustering, tiling, padding, presence, override, JSON round trip.
- `tests/render/test_crop_path.py`: stationary default, global motion, offset, tracking within speed and bounds, no
  jitter, split → fit, the expression evaluated against the keyframes, motion profiles.
- `tests/render/test_layout.py`: candidate order, geometry, stacked placement with a banner, fallbacks with reasons,
  forced-layout refusals, manual box, tracking render, and a real ffmpeg stacked + tracking render.
- Invariant 3 layout-3 tests, including every layout with a banner overlay keeping all boxes apart.
- `tests/qa/test_layout_checks.py`, `tests/pipeline/test_relayout.py` (state guards, copy kept, preview due,
  facecam stored once, failure keeps the old file), preview caption, `cf source facecam`, CLI, migration 0004.

Results: the full suite on the Mac; the render, QA and compliance tests inside the clipforge image (Debian ffmpeg
5.1, mediapipe), with no skips; the full suite in the image.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
