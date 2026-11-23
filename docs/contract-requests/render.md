# Contract requests from `feat/render` (Phase 1 C)

No change to `schemas.py` is needed. The requests below touch files that Phase 1 branches may not edit.

## 1. Dockerfile: copy `assets/` into the image (required)

Render burns captions with libass using the fonts bundled in `assets/fonts/`. Face detection loads
`assets/models/blaze_face_short_range.tflite`. Today the Dockerfile copies only `src`, `alembic.ini` and `scripts`,
so the image would have neither, and `render_candidate` would refuse to render ("font not bundled").

Proposed (next to `COPY scripts ./scripts`):

```dockerfile
COPY assets ./assets
```

`clipforge.render.assets.assets_dir()` looks in the repo checkout first, then in `<cwd>/assets` (`/app/assets` in
the image, since `WORKDIR /app`). It would also be good to extend the build-time import check to run
`python -c "from clipforge.render.assets import missing_fonts; assert not missing_fonts(['Montserrat-ExtraBold','Inter-Bold','Inter-Black'])"`.

## 2. Optional: an explicit assets location in config

If Phase 2 prefers configuration to discovery, add `CLIPFORGE_ASSETS_DIR` to `config.RuntimeEnv` (default
`assets`) and pass it to `render.assets`. Not needed for v1; the discovery above works in the repo, in CI and in
`/app`.

## 3. What the pipeline must pass to render (no schema change)

`clipforge.render.clip.render_candidate(ClipRequest, AppConfig)` needs:

| ClipRequest field | Source in Phase 2 |
| --- | --- |
| `candidate_id`, `start_s`, `end_s` | `candidates` row |
| `source_path` | `sources.local_path` |
| `words` | the source's `schemas.Transcript.words` (words outside the range are ignored) |
| `hook` | `Candidate.copy_.hook` |
| `content_kind` | `Campaign.content_kind` (`game` → fit_blur everywhere, PRD F16) |
| `account_set` | the account set the clip is for (picks `style_preset`) |
| `out_dir` | e.g. `settings.media_dir / "clips" / <campaign_slug>` |

It returns `schemas.RenderManifest` and writes `<clip>.mp4`, `<clip>.ass` and `<clip>.manifest.json`. QA is
`clipforge.qa.checks.qa_clip(manifest, campaign, platforms, captions, fps=app.render.fps, enabled=account.platforms)`.
Here `captions` are the `compliance.normalize_caption` outputs per platform. Store `report.model_dump_json()` in
`clips.qa_report_json`, then transition RENDERED → PENDING_REVIEW or QA_FAILED with an `events` row.
