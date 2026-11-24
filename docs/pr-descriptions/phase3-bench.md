# Phase 3 P3.1: render benchmark (`feat/phase3-bench` → `main`)

P3.1 asks whether a nightly run of 2 one-hour sources finishes in under 3 hours on the Intel Mac. **It does, with
about 17× to 23× headroom, so no render setting was changed.** This PR adds per-stage timing and a projection to
`scripts/bench_render.py` and records the first numbers.

Nothing under `src/` changed. Output spec (1080×1920, 30 fps, x264 `veryfast` CRF 21, loudnorm, burned captions, hook,
"Ad" label for the first 3 s) and every compliance invariant are untouched. No `.env`, no `PUBLISH_MODE`, no video
committed (`tests/fixtures/local/` is git-ignored).

## **Read this first: the sources were synthetic**

There is no real campaign file yet. The numbers come from:

- `nasa_terra_1999.mp4`: a 1999 NASA documentary, 530×402 (4:3), 48.6 min, 29.97 fps, 0.43 Mbps.
- `bench_1080p.mp4`: the same video scaled and pillarboxed to 1920×1080 (16:9), x264 `veryfast` at 8 Mbps (measured
  8.06 Mbps overall), 29.97 fps, AAC 128k, 48.6 min, 2.9 GB. Made inside the clipforge image with ffmpeg.

What this does and does not tell us:

- The 1080p file is up-scaled, soft, low-motion footage with black bars. Its decode cost is realistic, because
  8 Mbps H.264 at 1080p is typical. The x264 encode of the 1080×1920 output is not: that cost depends on how busy the
  picture is, and real gameplay is busier than this. I did not stress that. The useful number is how much slower the
  render can get before the night misses 3 hours: **about 17×** in the worst case below.
- 29.97 fps only. Twitch sources are often 60 fps. Decode-bound stages (`shots`, `face`, and the decode half of
  `encode`) will cost more per second of output. **Not measured.**
- Captions are synthetic words at 2.5 words/s (about 150 wpm), so libass burns realistic caption events. The talk
  runs use the real MediaPipe detector on this footage; how often it finds a face affects the crop path, not the
  detection cost.
- Not measured, because they leave the machine or cost money: the **Groq** upload and call, the **Anthropic** ranking
  and copy calls, and the **faster-whisper local fallback** (needs a ~1.6 GB model download; see "Risk" below).

Re-run on the first real campaign file in P3.2 before trusting these as final.

## How it was run

Inside the `clipforge:latest` image (Debian bookworm ffmpeg 5.1, as production), `linux/amd64`, on the Intel
i9-9880H MacBook, one container at a time, nothing else running:

- `--cpus 6`, the worker's limit in `docker-compose.yml`. The compose file sets **no memory limit**, so none was set;
  Docker Desktop's VM has 12.5 GB.
- The repo's `src/`, `scripts/` and `assets/` were mounted read-only over the image (`PYTHONPATH=/repo/src`), so the
  numbers are for exactly this branch.

```bash
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
docker run --rm --platform linux/amd64 --cpus 6 -e PYTHONPATH=/repo/src -w /repo \
  -v $PWD/src:/repo/src:ro -v $PWD/scripts:/repo/scripts:ro -v $PWD/assets:/repo/assets:ro \
  -v $PWD/config/clipforge.example.yaml:/repo/config/clipforge.example.yaml:ro \
  -v $PWD/tests/fixtures/local:/repo/tests/fixtures/local:ro \
  --entrypoint python clipforge:latest \
  scripts/bench_render.py /repo/tests/fixtures/local/bench_1080p.mp4 \
  --windows 4 --length 60 --kind game --audio     # then again with --kind talk
```

Four 60 s clips per run, spread evenly over the file. Each run renders through the real `render_candidate`
(scene detection, face tracking for `talk`, one ffmpeg run, ffprobe). The script then checks the manifest still has the
"Ad" label from 0.0 s to at least 3.0 s and refuses to report timings if not.

## Results (baseline = final: no render change was needed)

**Stages that scale with source length** (measured on the whole 48.6 min file, then divided by its minutes; download
not included):

| Stage | 530×402 source | 1080p source |
| --- | --- | --- |
| SHA-256 of the source (ingest) | 0.01 s per source minute | 0.17 s per source minute |
| Audio extract and chunk for transcription | 0.24 s per source minute | 0.31 s per source minute |

**Stages that scale with the number of clips, not the source length** (the encoder seeks straight to the clip start,
so a window at 47 min cost the same as one at 0). Seconds of processing per second of output clip, then per 60 s clip:

| Stage | 530×402 game | 530×402 talk | 1080p game | 1080p talk |
| --- | --- | --- | --- | --- |
| Shot detection (PySceneDetect) | 0.031 | 0.026 | 0.078 | 0.077 |
| Face tracking (MediaPipe, 5 fps sampling) | n/a | 0.035 | n/a | 0.099 |
| Encode (crop/blur, libass, x264, loudnorm) | 0.413 | 0.384 | 0.468 | 0.429 |
| ffprobe | 0.003 | 0.003 | 0.003 | 0.003 |
| QA re-probe and SHA-256 of the clip | 0.002 | 0.002 | 0.002 | 0.002 |
| Other (ASS build etc.) | 0.001 | 0.004 | 0.000 | 0.004 |
| **Total, s per output second** | **0.450** | **0.454** | **0.552** | **0.614** |
| **Total, s per 60 s clip** | 27.0 | 27.2 | 33.1 | 36.8 |

Same stages expressed per source minute of a one-hour source with 8 clips of 60 s (the `top_n_per_source`
worst case, every candidate rendered):

| Stage (s per source minute) | 1080p game | 1080p talk |
| --- | --- | --- |
| SHA-256 | 0.17 | 0.17 |
| Audio extract and chunk | 0.31 | 0.31 |
| Shot detection | 0.62 | 0.62 |
| Face tracking | n/a | 0.79 |
| Encode | 3.75 | 3.43 |
| ffprobe + QA | 0.04 | 0.04 |
| Other | 0.00 | 0.03 |
| **Total** | **4.9** | **5.4** |

### Projection: two one-hour 1080p sources

Worst case: both sources, all 8 candidates each rendered (16 clips of 60 s), plus hashing and audio extraction.

| Case | Per-source stages | Render | Night total | Headroom vs 3 h |
| --- | --- | --- | --- | --- |
| 1080p game, 16 × 60 s | 1.0 min | 8.8 min | **9.8 min** | 18× |
| 1080p talk, 16 × 60 s | 1.0 min | 9.8 min | **10.8 min** | 17× |
| 1080p talk, 16 × 75 s (`window_max_s`) | 1.0 min | 12.3 min | **13.2 min** | 14× |
| 530×402 talk, 16 × 60 s | 0.5 min | 7.3 min | **7.8 min** | 23× |

The pipeline stops once `nightly_candidate_target` (8) clips pass QA, so a normal night renders fewer than 16.

### Before / after

| Run | Change | 1080p talk, s per output second | Night projection |
| --- | --- | --- | --- |
| baseline | none | 0.614 | 10.8 min |
| after | none: the goal (under 3 h) was met with about 17× headroom, so I did not tune. Changing encoder preset, CRF or face sampling would risk the visible output for no benefit. | n/a | n/a |

## Risk (not covered by these numbers)

The **faster-whisper local fallback** runs `large-v3-turbo` int8 on the CPU, and the code comments call it "slow". It
only runs after Groq fails twice on a chunk. If it ran for two one-hour sources, it could cost more than everything
measured here combined, and it is the one stage that might threaten the 3 hour goal. I did not measure it: it needs a
~1.6 GB model download from Hugging Face and was outside this task. Worth measuring before relying on the fallback.
Groq itself is network-bound and was also not measured.

## Changes in this PR

- `scripts/bench_render.py`:
  - stage timings (`shots`, `face`, `encode`, `probe`, `qa_io`, `other`) through the injectable hooks of
    `render_candidate`, so no `src/` change;
  - `--audio` times hashing and audio extraction on the whole source;
  - synthetic caption words when no `--transcript` is given;
  - a nightly projection (`--sources`, `--source-minutes`, `--clips-per-source`) and `--json`;
  - it stops if the manifest loses the "Ad" label.
  Existing arguments and the "seconds per output second:" line are unchanged.
- `tests/render/test_bench_render.py`: tests for the pure helpers (`synthetic_words`, `project_nightly`, `Timers`) and
  the smoke test now also expects the projection line. No existing test was weakened.
- `docs/TASKS.md`: P3.1 ticked, with the result and the note to re-run on a real file.

## Verification

- `make lint test`: ruff clean; 1125 passed, 11 skipped, 1 deselected.
- The script was run end to end in the clipforge image (the bench smoke test skips on the Mac, which has no ffmpeg).
- compliance-reviewer on the diff: **PASS** for invariants 1–12 and the "also enforced" rules. `src/` has an empty diff,
  no media is staged, and the script now stops if the manifest loses the "Ad" label. Its one wording note was applied.
