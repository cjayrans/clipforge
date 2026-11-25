"""Render one candidate end to end: shots → crop plan → ASS (captions + hook + "Ad" label) → encode → manifest.

DB-free on purpose: Phase 2 `pipeline.py` creates the `clips` row, calls `render_candidate`, runs QA and performs
the state transitions (with `events` rows). Silent sources are refused (PRD F17: unsupported).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from clipforge import compliance
from clipforge.config import AppConfig
from clipforge.qa.probe import ProbeResult, probe
from clipforge.render import campaign_overlay
from clipforge.render.ass import ass_document, escape_text
from clipforge.render.assets import fonts_dir, missing_fonts, project_root
from clipforge.render.captions import build_captions
from clipforge.render.encode import (
    EncodeSettings,
    OverlayInput,
    RenderError,
    Runner,
    build_ffmpeg_args,
    maps_only_source_audio,
    run_ffmpeg,
    sha256_file,
)
from clipforge.render.overlays import AD_LABEL_STYLE, HOOK_STYLE, has_dark_box, overlay_events, overlay_styles
from clipforge.render.reframe import Detector, plan_crops
from clipforge.render.shots import Shot, detect_shots
from clipforge.schemas import CampaignOverlay, OverlayManifest, RenderManifest, Word

log = logging.getLogger(__name__)

DetectorFactory = Callable[[Path], contextlib.AbstractContextManager[Detector | None]]


@dataclass(frozen=True)
class ClipRequest:
    candidate_id: int
    source_path: Path
    start_s: float  # source seconds
    end_s: float
    words: Sequence[Word]  # transcript words (source time); only those inside the range are used
    hook: str
    content_kind: Literal["talk", "game"]
    account_set: str
    out_dir: Path
    overlay: CampaignOverlay | None = None  # the campaign's overlay image, if it has one


@contextlib.contextmanager
def mediapipe_detector(source: Path) -> Iterator[Detector | None]:
    """MediaPipe face detector when available (Linux/Docker), else None → fit_blur everywhere."""
    try:
        from clipforge.render.face_detect import MediaPipeFaceDetector

        detector = MediaPipeFaceDetector(source)
    except ImportError:
        log.warning("mediapipe not installed; reframing falls back to fit_blur")
        yield None
        return
    with detector:
        yield detector


def render_candidate(
    req: ClipRequest,
    app: AppConfig,
    *,
    detector_factory: DetectorFactory = mediapipe_detector,
    shots_fn: Callable[[Path, float, float], list[Shot]] = detect_shots,
    probe_fn: Callable[[Path], ProbeResult] = probe,
    run: Runner = run_ffmpeg,
) -> RenderManifest:
    duration = round(req.end_s - req.start_s, 3)
    compliance.assert_duration_bounds(duration)
    if not app.pipeline.min_clip_s <= duration <= app.pipeline.max_clip_s:
        raise RenderError(
            f"clip length {duration}s outside config {app.pipeline.min_clip_s}-{app.pipeline.max_clip_s}s"
        )
    account = app.account_sets.get(req.account_set)
    if account is None:
        raise RenderError(f"unknown account set {req.account_set!r}")
    style = app.styles[account.style_preset]
    missing = missing_fonts([style.font])
    if missing:
        raise RenderError(f"style {account.style_preset!r} font not bundled in assets/fonts: {missing}")

    src = probe_fn(req.source_path)
    if src.audio_streams == 0:
        raise RenderError("source has no audio stream (silent footage is unsupported, PRD F17)")
    if not src.width or not src.height:
        raise RenderError("source has no video stream")
    if req.end_s > src.duration_s + 0.5:
        raise RenderError(f"clip ends at {req.end_s}s but the source is {src.duration_s:.1f}s long")

    shots = shots_fn(req.source_path, req.start_s, req.end_s)
    if req.content_kind == "game":
        plan = plan_crops(shots, src.width, src.height, "game", None)
    else:
        with detector_factory(req.source_path) as detector:
            plan = plan_crops(shots, src.width, src.height, "talk", detector)

    caption_style, caption_evts = build_captions(req.words, req.start_s, req.end_s, style)
    if not escape_text(app.render.ad_label_text):
        raise RenderError("render.ad_label_text is blank; the 'Ad' label must be visible (invariant 3)")
    # Campaign overlay: placed and checked (never covers captions, hook or platform UI); the "Ad" label goes to the
    # first spot clear of it (invariant 3). Any conflict refuses the render; nothing is shrunk or moved silently.
    placement = None
    try:
        if req.overlay is not None:
            placement = campaign_overlay.resolve(
                req.overlay, project_root(), app.styles.values(), app.render.ad_label_text
            )
        ad_at = campaign_overlay.place_ad_label(app.render.ad_label_text, placement, app.styles.values())
    except campaign_overlay.OverlayPlacementError as exc:
        raise RenderError(f"campaign overlay: {exc}") from exc
    overlay_evts = overlay_events(req.hook, app.render, ad_label_at=(ad_at.alignment, ad_at.x, ad_at.y))
    ad = next(e for e in overlay_evts if e.style == AD_LABEL_STYLE)
    hook = next((e for e in overlay_evts if e.style == HOOK_STYLE), None)
    styles = [caption_style, *overlay_styles(style)]
    doc = ass_document(styles, [*caption_evts, *overlay_evts])
    # Invariant 3: claim the label only if its exact Dialogue line and its style are in the file libass will burn.
    doc_lines = doc.splitlines()
    has_ad_label = ad.line() in doc_lines and any(line.startswith(f"Style: {AD_LABEL_STYLE},") for line in doc_lines)
    # The dark box is claimed only if the exact style line that draws it is in the file.
    ad_style = next(st for st in styles if st.name == AD_LABEL_STYLE)
    ad_label_box = has_dark_box(ad_style) and ad_style.line() in doc_lines

    req.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"clip_{req.candidate_id}"
    clip_path = req.out_dir / f"{stem}.mp4"
    ass_path = req.out_dir / f"{stem}.ass"
    ass_path.write_text(doc, encoding="utf-8")

    overlay_input: OverlayInput | None = None
    overlay_manifest: OverlayManifest | None = None
    if placement is not None and req.overlay is not None:
        start = 0.0 if req.overlay.full_duration else min(app.render.hook_seconds, duration)
        overlay_input = OverlayInput(
            path=placement.path,
            width=placement.width,
            height=placement.height,
            x=placement.rect.x0,
            y=placement.rect.y0,
            start_s=start,
        )
        overlay_manifest = OverlayManifest(
            image_sha256=placement.sha256,
            rect=placement.rect,
            opaque_rect=placement.opaque_rect,
            start_s=start,
            end_s=duration,
            full_duration=req.overlay.full_duration,
        )

    planned = RenderManifest(
        clip_path=str(clip_path),
        candidate_id=req.candidate_id,
        account_set=req.account_set,
        style_preset=account.style_preset,
        width=app.render.width,
        height=app.render.height,
        fps=float(app.render.fps),
        duration_s=duration,
        has_ad_label=has_ad_label,
        ad_label_start_s=ad.start_s,
        ad_label_end_s=ad.end_s,
        hook_text=req.hook.strip(),
        hook_end_s=hook.end_s if hook else 0.0,
        audio_source_only=False,
        sha256="",
        ad_label_rect=ad_at.rect,
        ad_label_layout=compliance.AD_LABEL_LAYOUT,
        ad_label_box=ad_label_box,
        overlay=overlay_manifest,
    )
    args = build_ffmpeg_args(
        planned,
        source_path=req.source_path,
        clip_start_s=req.start_s,
        crop_plan=plan,
        ass_path=ass_path,
        fonts_dir=fonts_dir(),
        settings=EncodeSettings.from_config(app.render),
        overlay=overlay_input,
    )
    audio_only = maps_only_source_audio(args)
    if not audio_only:
        raise RenderError("encoder would map audio other than the source track (invariant 10)")

    if placement is not None and sha256_file(placement.path) != placement.sha256:
        raise RenderError("campaign overlay file changed after it was checked; render refused")
    log.info("rendering candidate %s (%.1fs, %d shots)", req.candidate_id, duration, len(plan.shots))
    run(args)

    out = probe_fn(clip_path)
    actual = round(out.duration_s, 3)
    if overlay_manifest is not None:  # eof_action=repeat keeps the image up until the last frame
        overlay_manifest = overlay_manifest.model_copy(update={"end_s": actual})
    manifest = planned.model_copy(
        update={
            "overlay": overlay_manifest,
            "duration_s": actual,
            "sha256": sha256_file(clip_path),
            "audio_source_only": audio_only,
        }
    )
    compliance.assert_render_manifest(manifest)
    clip_path.with_suffix(".manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest
