## Facecam detection v2: sample the clip windows, skip full-screen camera frames, reject static faces

The first facecam check on the Boeshi source (#14) went wrong. The debug still was a mid-VOD frame from a
full-screen camera segment: no gameplay, the facecam corner showing only dark wall. Detection had sampled
the whole VOD, ignored large faces without telling them apart from gameplay, and never checked that the face it
found was a live person. This PR fixes all three.

### What changes
- **Samples come from the clip windows.** The 40 samples are taken inside the source's candidate windows, spread by
  their length. The whole source is sampled only when it has no candidates. (`sample_times_in_windows`,
  `ingest.models.clip_windows`)
- **Each sample is classified.** A confident face at least 25 % of the frame height means a **full-screen camera**
  frame. Large faces are looked for on the whole frame and on frame-height tiles. Every other frame is
  **gameplay**. Only gameplay samples count toward the facecam; with fewer than 8, the source has no facecam.
  (`classify_frame`, `detect_large`, `sample_frame`)
- **Static faces are rejected.** For the candidate face box, the face region of every hit frame is converted to
  grey, resized to 32×32 and its mean brightness removed. The mean per-pixel standard deviation across frames
  (`face_variation`) must be ≥ 6 grey levels. A face in a picture on the wall scores about 0, and a lighting change
  alone stays under 6. The face must also be in ≥ 60 % of gameplay samples (was 40 % of all samples).
- **The result records why.** Each detection stores the gameplay / full-screen camera counts, the variation, the
  reason when nothing is found, and a representative frame time for the debug still. With no valid facecam the
  source stores "none" and `layout: auto` resolves to crop. `DETECTOR_VERSION` is now `facecam-v2`, so stored v1
  results are ignored and detected again.
- **Each clip decides from its own frames.** Stacked is refused for a clip whose whole frames are more than 50 %
  full-screen camera, or whose facecam face never changes. The presence check (a face in ≥ 60 % of the clip's
  facecam crops) is unchanged. The reason goes to `layout_reason` and the preview.
- **`cf source facecam` changes.** It gets `--no-store`, which shows a detection without saving it. The debug still
  now uses the detector's own representative frame (a hit, else a gameplay frame) instead of the middle of the VOD.

### Source 2 (Boeshi), checked with this code
The run used `--redetect --no-store` in a one-off container with this branch's `src/`, so nothing was saved, nothing
was re-rendered, the Boeshi layout setting was not changed, and the running containers were not touched.
- 40 samples inside 24 clip windows: 29 gameplay and 11 full-screen camera.
- The face was in 29 of 29 gameplay frames, with variation 45.2 (a live face).
- Facecam box x 0, y 216, 376×222 px of 1920×1080; median face x 96, y 269, 93×93.

The debug still (t ≈ 577 s) shows GTA gameplay with Boeshi's facecam in the top-left corner under the chat. The
face box is on his face and the facecam box covers the cam. It trims a little of the cam's top and right edge and
takes in a thin strip of gameplay along the bottom. The stacked mock (his face on top, gameplay below) is usable.
The v1 box was at almost the same place; v1's real faults were the frame it showed and that it did not know about
full-screen camera segments.

### Tests
- `tests/render/test_facecam.py` covers each failure with a synthetic fixture:
  - a **static printed face**: unit test, plus end to end on synthetic frames whose face never changes;
  - a **full-screen camera segment**: unit test, plus end to end on a full-screen camera fixture;
  - a VOD whose first half is full-screen camera: sampling the clip windows finds the facecam.
- Also in `test_facecam.py`: `classify_frame`, `face_variation` (live, printed, lighting-only), window sampling,
  presence variation, and the clip full-screen ratio.
- `tests/render/test_layout.py`: stacked is skipped for a full-screen camera clip and for a static face, and kept
  for a live face in gameplay.
- `tests/pipeline/test_relayout.py`: detection receives the source's clip windows.
- `tests/ingest/test_source_facecam.py`: `--no-store` saves nothing and samples the clip windows.

Not calibrated yet: `MIN_FACE_VARIATION = 6` and the 25 % full-screen threshold were set from synthetic data and
this one source (live face 45.2). They are module constants.

### Compliance review
The compliance-reviewer subagent found no blocking issues. Both of its notes are fixed, and both now fail closed:
- a probe failure inside the per-clip presence check sets the face ratio to 0, so stacked is never chosen on
  unchecked frames;
- a facecam face whose movement could not be measured refuses stacked (test added).

### Compliance
No compliance rule changes. Render placement, the "Ad" label, captions, overlays and publishing are untouched.
`compliance.py` and `schemas.py` are not changed, and there is no migration (the new fields live in
`sources.facecam_json`).

### Not done here (on purpose)
Not merged. `make up` was not run and no container was restarted (tonight's posting window). No re-render.
Boeshi stays on `layout: crop`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
