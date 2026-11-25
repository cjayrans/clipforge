# "Ad" label: out of the platform UI, under the hook, on a dark box (`feat/ad-label-position` → `main`)

Written for Carson.

The "Ad" label used to sit at the top-left corner, at (48, 120). That is inside TikTok's top 150 px UI zone, and it
had a faint box: clip 11 of the Boeshi run was hard to read. This PR moves it and gives it a stronger box.
**Nothing was re-rendered and nothing was deployed.** Clips already rendered keep their old label. New renders get the
new one, including a New hook or Shorter re-render of a pending clip.

## Where it goes now

Carson chose this spot: **the left edge, just under the hook's largest possible box. The default is (48, 548), and the
label box covers x 36–132, y 536–620.**

- **Why not y≈180:** the hook is centred at y 346. Its worst case (3 lines at the largest style preset) spans
  y 166–525, and a 2-line hook already reaches up to y≈220. Between TikTok's 150 px zone and the hook there are only
  about 16 px, so a label at y≈180 would sit inside every multi-line hook.
- **What the spot clears:**
  - the hook (with an 8 px margin)
  - the caption band (y 1134–1401)
  - the TikTok UI zones (top 150 px, right 15 %, bottom 20 %)
  - the Boeshi overlay's visible area (x 266–814, y 541–673): the label ends at x 132
  - Instagram's Reels header
- **On game footage**, the spot is in the blurred band just above the gameplay.
- **Visible from 0.0 s to `render.ad_label_seconds`** (3.0 s, never less) as before.

## Readability

- The box is BorderStyle 3 with white text on a **75 %-opaque black box** (`&H40000000`) and 12 px padding. Before, it
  was about 69 % opaque with 10 px padding.
- Measured in the clipforge image on a pure white source:
  - Inside the label box the typical luma is **64**, and the letters are **255**.
  - After 3 s the area is back to **255** (the label is gone).
- On the regular test clip, the label now changes its area by a mean grey diff of 111. The old corner label changed
  its area by 49.

## Placement and checks (`render/campaign_overlay.py`, `render/overlays.py`)

- `place_ad_label(text, overlay, styles)` tries these spots in order:
  1. the default (48, 548);
  2. the left edge just under the hook's worst case, if a bigger style preset pushes it lower;
  3. the left edge just under a campaign overlay's visible area.
- Each spot must miss the hook band, the caption band, every platform UI zone and the overlay's visible pixels. If no
  spot does, the render is refused. `cf campaign add` runs the same check, so a campaign overlay that leaves the
  label nowhere to go fails at registration.
- The platform UI zones now have one source, `compliance.ui_zones()`, used by both the overlay and the label checks.

## Manifest and compliance (contract change: `schemas.RenderManifest`)

- New fields:
  - `ad_label_layout`: defaults to 1. New renders write 2 (`compliance.AD_LABEL_LAYOUT`).
  - `ad_label_box`: True only if the exact style line that draws the dark box is in the written ASS file.
  - `ad_label_rect` already existed; layout 2 requires it.
- `compliance.ad_label_placement_problems` runs inside `render_manifest_problems`, so QA and `assert_render_manifest`
  apply it. For a layout-2 manifest it requires:
  - a recorded label box,
  - inside the frame,
  - outside every platform UI zone,
  - `ad_label_box` true.
- **Layout-1 manifests**, meaning clips rendered before this PR (including approved clip 1), keep only the existing
  checks: label present, from 0.0 s, until ≥ 3.0 s. They are not re-judged by rules they were not rendered under. QA
  only re-runs on an existing file after a failed re-render.
- The 3-second rule applies to every manifest, as before.
- `docs/COMPLIANCE.md` invariant 3 describes the new placement, box and versioning, and lists the new tests.

## Tests

- `tests/compliance/test_inv03_ad_label.py`:
  - a layout-2 label under the top, right or bottom UI zone fails;
  - a missing box fails, and a missing rect fails;
  - the 3 s rule still applies;
  - older manifests keep their rules;
  - new renders use layout 2;
  - the label is never drawn under an overlay (it is moved, or the render is refused).
- `tests/render/test_campaign_overlay.py`:
  - default spot and the reasons for it;
  - the label stays put with a clear overlay, and moves under an overlay that covers its spot;
  - refused when no spot is clear;
  - follows a bigger hook style;
  - the dark-box style check.
- `tests/render/test_clip.py`:
  - the manifest records layout 2, the box and a rect below 150 px;
  - the real-pixel label test now reads the label box from the manifest;
  - new `test_ad_label_readable_on_white_footage` (needs ffmpeg).
- Existing tests rewritten on purpose: the four placement tests that asserted the old corner and the top-right
  fallback. No assertion in `tests/compliance/` was removed or relaxed.

## Verification

- `make lint test`: ruff clean; 1285 passed, 13 skipped, 1 deselected. The skips need ffmpeg; they run in CI and the
  image.
- `mypy --strict` on `schemas.py` and `compliance.py`: clean.
- In the `clipforge:latest` image (Debian ffmpeg 5.1), `tests/render`, `tests/qa` and `tests/compliance`: 452 passed,
  0 skipped.
- With the real config and Boeshi overlay: label at (48, 548), box x 36–132, y 536–620, clear of the overlay.
- compliance-reviewer, pass 1: **FAIL** on two test items. Invariant 3 was preserved and stronger, and every other
  invariant was untouched.
  1. The overlay "never covered" test had a `try/except` that would also have passed if every overlay were refused.
     Fixed: cases 1–3 must place, at y 548, 660 and 548, and only case 4 is expected to be refused.
  2. `test_new_renders_use_layout2` didn't render anything. Fixed: it now calls `render_candidate` (ffmpeg faked) and
     checks the manifest's layout, box and rect.
  - Also added `test_layout2_label_never_covered_by_campaign_overlay`, and the style test uses `dataclasses.replace`.
- compliance-reviewer, pass 2: **PASS** for invariants 1–12 (tests/compliance: 241 passed, 1 skipped on the Mac). Its
  condition was to run the ffmpeg tests in the image after the fixes. Done: tests/render, qa and compliance gave
  **452 passed, 0 skipped** in `clipforge:latest`.
- Contract note: `schemas.RenderManifest` gains `ad_label_layout` (default 1) and `ad_label_box` (default False).

## Follow-ups the reviewer suggested (not in this PR)

- The manifest-level check (QA) tests the label against the platform UI zones but not against the hook or caption
  bands. Only the renderer checks those, through `ad_label_problems`. Adding them to QA needs the style presets in
  `compliance`.
- A manifest JSON with `ad_label_layout` removed by hand would load as layout 1 and skip the new checks. The same was
  already true of `has_ad_label`. Pinning the carve-out to the known old manifests would close it.
- **Approved clip 1 keeps the old label at y 110–190, partly under TikTok's top 150 px.** Confirm that's acceptable
  before you approve it for publishing, or reject it and re-run.
- CI: see the PR checks.

## Not done (on purpose)

- No clip was re-rendered. The 11 pending clips and approved clip 1 still have the old corner label. Approving clip 1
  publishes it as rendered, and pending clips get the new label only on New hook or Shorter.
- The stack was not rebuilt. Until `make up`, the running worker and bot still render with the old label.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
