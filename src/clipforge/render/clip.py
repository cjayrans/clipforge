"""Render one candidate end to end: shots → layout + crop plan → ASS (captions + hook + "Ad" label) → encode →
manifest.

DB-free on purpose: Phase 2 `pipeline.py` creates the `clips` row, calls `render_candidate`, runs QA and performs
the state transitions (with `events` rows). Silent sources are refused (PRD F17: unsupported).

Layouts (feat/reframe, `render.layout`): game footage tries the campaign's layout candidates in order (auto: stacked
when a facecam is known, then crop, then fit). A candidate is skipped when its prerequisites fail (stacked: a face in
the facecam box in at least `STACKED_MIN_FACE_RATIO` of the clip's sampled frames; crop: the source is wider than the
crop) or when the hook, "Ad" label, captions and campaign overlay cannot all be placed (`place_texts`). Why each was
skipped goes into `manifest.layout_reason`. Talk footage keeps the face-crop planner (`render.reframe`).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from clipforge import compliance
from clipforge.config import AppConfig
from clipforge.qa.probe import ProbeResult, probe
from clipforge.render import campaign_overlay, crop_path
from clipforge.render import layout as layouts
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
from clipforge.render.facecam import (
    MIN_FACE_VARIATION,
    STACKED_MAX_FULLCAM_RATIO,
    FacecamSpec,
    Presence,
    default_presence,
)
from clipforge.render.overlays import AD_LABEL_STYLE, HOOK_STYLE, has_dark_box, overlay_events, overlay_styles
from clipforge.render.reframe import Detector, fit_blur, plan_crops
from clipforge.render.saliency import MotionSample, motion_samples
from clipforge.render.shots import Shot, detect_shots
from clipforge.schemas import (
    CampaignOverlay,
    CropMode,
    CropPlan,
    LayoutName,
    OverlayManifest,
    PxBox,
    Rect,
    RenderedLayout,
    RenderManifest,
    ShotCrop,
    Word,
)

log = logging.getLogger(__name__)

DetectorFactory = Callable[[Path], contextlib.AbstractContextManager[Detector | None]]
PresenceFn = Callable[[Path, PxBox, float, float], Presence]  # (source, facecam box, start, end)
MotionFn = Callable[[Path, float, float, int, int, PxBox | None], list[MotionSample]]


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
    layout: LayoutName = "auto"  # the campaign's layout setting
    facecam: FacecamSpec | None = None  # detected or campaign-set facecam box, if any


@dataclass
class _Choice:
    layout: RenderedLayout
    texts: campaign_overlay.TextPlacement
    face_rect: Rect | None = None
    facecam_rect: Rect | None = None
    reasons: list[str] = field(default_factory=list)


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


def _choose_layout(
    req: ClipRequest,
    app: AppConfig,
    src_w: int,
    src_h: int,
    facecam_box: PxBox | None,
    placement: campaign_overlay.OverlayPlacement | None,
    presence_fn: PresenceFn,
    timings: dict[str, float],
) -> _Choice:
    """The first layout candidate whose prerequisites and placement rules all hold (see module docstring)."""
    styles = list(app.styles.values())
    text = app.render.ad_label_text
    skipped: list[str] = []
    for name in layouts.candidates(req.layout, req.content_kind, facecam_box is not None):
        try:
            if name == "stacked":
                if facecam_box is None:
                    raise layouts.LayoutUnavailable("no facecam found or set for this source")
                if not layouts.croppable("stacked", src_w, src_h):
                    raise layouts.LayoutUnavailable("source is too narrow for the gameplay panel")
                t0 = time.monotonic()
                presence = presence_fn(req.source_path, facecam_box, req.start_s, req.end_s)
                timings["facecam_presence"] = round(time.monotonic() - t0, 2)
                if presence.face is None or presence.ratio < layouts.STACKED_MIN_FACE_RATIO:
                    detail = f" ({presence.problem})" if presence.problem else ""
                    raise layouts.LayoutUnavailable(
                        f"a face is in the facecam box in {presence.ratio:.0%} of {presence.samples} sampled frames, "
                        f"needs {layouts.STACKED_MIN_FACE_RATIO:.0%}{detail}"
                    )
                if presence.fullcam_ratio > STACKED_MAX_FULLCAM_RATIO:
                    raise layouts.LayoutUnavailable(
                        f"the clip is a full-screen camera shot in {presence.fullcam_ratio:.0%} of its frames"
                    )
                if presence.variation is None:
                    raise layouts.LayoutUnavailable("the facecam face could not be checked for movement")
                if presence.variation < MIN_FACE_VARIATION:
                    raise layouts.LayoutUnavailable(
                        f"the face in the facecam box never changes (variation {presence.variation:.1f}): "
                        "a picture, not a person"
                    )
                face = layouts.face_rect_out(presence.face, facecam_box)
                panel = layouts.facecam_panel()
                texts = campaign_overlay.place_texts(text, placement, styles, face=face, panel=panel)
                return _Choice("stacked", texts, face_rect=face, facecam_rect=panel, reasons=skipped)
            if name == "crop" and req.content_kind == "game" and not layouts.croppable("crop", src_w, src_h):
                raise layouts.LayoutUnavailable("source is not wider than 9:16")
            texts = campaign_overlay.place_texts(text, placement, styles)
            return _Choice(name, texts, reasons=skipped)
        except layouts.LayoutUnavailable as exc:
            skipped.append(f"{name}: {exc}")
        except campaign_overlay.OverlayPlacementError as exc:
            skipped.append(f"{name}: campaign overlay: {exc}" if placement is not None else f"{name}: {exc}")
    raise RenderError("no layout fits this clip: " + "; ".join(skipped))


def _plan_game(
    req: ClipRequest,
    choice: _Choice,
    shots: Sequence[Shot],
    src_w: int,
    src_h: int,
    facecam_box: PxBox | None,
    motion_fn: MotionFn,
    timings: dict[str, float],
) -> tuple[CropPlan, float | None, str]:
    """Crop plan for game footage in the chosen layout. Returns (plan, max pan speed, note)."""
    if choice.layout == "fit":
        return plan_crops(shots, src_w, src_h, "game", None), None, ""
    gw, gh = layouts.gameplay_crop(choice.layout, src_w, src_h)
    gy = (src_h - gh) // 2
    t0 = time.monotonic()
    try:
        samples = motion_fn(req.source_path, req.start_s, req.end_s, src_w, src_h, facecam_box)
    except (OSError, ValueError) as exc:
        log.warning("candidate %s: motion pass failed (%s); stationary centre crop", req.candidate_id, exc)
        samples = []
    timings["saliency"] = round(time.monotonic() - t0, 2)
    max_pan = crop_path.max_pan_px_s(src_w)
    crops: list[ShotCrop] = []
    notes: list[str] = []
    for shot in shots:
        off = shot.start_s - req.start_s
        end = off + shot.duration_s
        rel = [MotionSample(t=round(m.t - off, 3), profile=m.profile) for m in samples if off <= m.t < end]
        plan = crop_path.plan_path(rel, shot.duration_s, src_w, gw, allow_fit=choice.layout == "crop", max_pan=max_pan)
        if plan.fit:
            crops.append(fit_blur(shot, src_w, src_h))
            notes.append(f"shot {shot.start_s:.1f}s fit ({plan.reason})")
            continue
        crops.append(
            ShotCrop(
                start_s=shot.start_s,
                end_s=shot.end_s,
                mode=CropMode.STACKED if choice.layout == "stacked" else CropMode.GAME_CROP,
                x=plan.x,
                w=gw,
                h=gh,
                y=gy,
                camera=plan.camera,
                keyframes=plan.keyframes,
                facecam=facecam_box if choice.layout == "stacked" else None,
            )
        )
        notes.append(f"shot {shot.start_s:.1f}s {plan.camera} ({plan.reason})")
    return CropPlan(src_w=src_w, src_h=src_h, shots=crops), max_pan, "; ".join(notes)


def render_candidate(
    req: ClipRequest,
    app: AppConfig,
    *,
    detector_factory: DetectorFactory = mediapipe_detector,
    shots_fn: Callable[[Path, float, float], list[Shot]] = detect_shots,
    probe_fn: Callable[[Path], ProbeResult] = probe,
    run: Runner = run_ffmpeg,
    presence_fn: PresenceFn = default_presence,
    motion_fn: MotionFn = motion_samples,
) -> RenderManifest:
    started = time.monotonic()
    timings: dict[str, float] = {}
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
    if not escape_text(app.render.ad_label_text):
        raise RenderError("render.ad_label_text is blank; the 'Ad' label must be visible (invariant 3)")

    t0 = time.monotonic()
    shots = shots_fn(req.source_path, req.start_s, req.end_s)
    timings["shots"] = round(time.monotonic() - t0, 2)

    facecam_box: PxBox | None = None
    if req.facecam is not None and req.content_kind == "game":
        try:
            facecam_box = req.facecam.resolve(src.width, src.height)
        except ValueError as exc:
            raise RenderError(f"facecam box: {exc}") from exc

    # Campaign overlay: placed and checked per layout (never covers captions, hook, face or platform UI); the hook and
    # the "Ad" label go to the first spots clear of it (invariant 3). A conflict moves to the next layout candidate;
    # with none left the render is refused. Nothing is shrunk or moved silently.
    placement = None
    if req.overlay is not None:
        try:
            placement = campaign_overlay.load_placement(req.overlay, project_root())
        except campaign_overlay.OverlayPlacementError as exc:
            raise RenderError(f"campaign overlay: {exc}") from exc
    choice = _choose_layout(req, app, src.width, src.height, facecam_box, placement, presence_fn, timings)

    note = ""
    max_pan: float | None = None
    if req.content_kind == "game":
        plan, max_pan, note = _plan_game(req, choice, shots, src.width, src.height, facecam_box, motion_fn, timings)
        if choice.layout == "crop" and all(c.mode is CropMode.FIT_BLUR for c in plan.shots):
            choice.reasons.append("crop: every shot fell back to fit (salient action split across the frame)")
            choice.layout = "fit"
    else:
        t0 = time.monotonic()
        if choice.layout == "fit":
            plan = plan_crops(shots, src.width, src.height, "game", None)
        else:
            with detector_factory(req.source_path) as detector:
                plan = plan_crops(shots, src.width, src.height, "talk", detector)
        timings["faces"] = round(time.monotonic() - t0, 2)
        if all(c.mode is CropMode.FIT_BLUR for c in plan.shots):
            choice.layout = "fit"
    layout_reason = "; ".join([*choice.reasons, *([note] if note else [])])

    texts = choice.texts
    ad_at = texts.ad
    caption_style, caption_evts = build_captions(req.words, req.start_s, req.end_s, style)
    overlay_evts = overlay_events(
        req.hook, app.render, ad_label_at=(ad_at.alignment, ad_at.x, ad_at.y), hook_cy=texts.hook_cy
    )
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
        layout=choice.layout,
        layout_reason=layout_reason[:2000],
        hook_rect=texts.hook_rect,
        caption_rect=texts.caption_rect,
        face_rect=choice.face_rect if choice.layout == "stacked" else None,
        facecam_rect=choice.facecam_rect if choice.layout == "stacked" else None,
        crop_plan=plan,
        max_pan_px_s=max_pan,
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
    log.info(
        "rendering candidate %s (%.1fs, %d shots, layout %s)",
        req.candidate_id,
        duration,
        len(plan.shots),
        choice.layout,
    )
    t0 = time.monotonic()
    run(args)
    timings["encode"] = round(time.monotonic() - t0, 2)

    out = probe_fn(clip_path)
    actual = round(out.duration_s, 3)
    if overlay_manifest is not None:  # eof_action=repeat keeps the image up until the last frame
        overlay_manifest = overlay_manifest.model_copy(update={"end_s": actual})
    timings["total"] = round(time.monotonic() - started, 2)
    manifest = planned.model_copy(
        update={
            "overlay": overlay_manifest,
            "duration_s": actual,
            "sha256": sha256_file(clip_path),
            "audio_source_only": audio_only,
            "timings": timings,
        }
    )
    compliance.assert_render_manifest(manifest)
    clip_path.with_suffix(".manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest
