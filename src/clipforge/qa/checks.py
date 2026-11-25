"""QA gate: ffprobe + render manifest + captions + campaign → `schemas.QAReport` (ARCHITECTURE §4.5, PRD F6).

Failures never reach review: Phase 2 moves a clip to QA_FAILED unless `report.passed`.

Checks (one `QACheck` each, all run so the report lists every problem):
- `file`: the clip exists and its SHA-256 equals the manifest's (no stale or swapped file).
- `duration` (10–90 s, invariant 10) on the probed duration, and `duration_matches_manifest` (±0.5 s).
- `resolution` 1080×1920, `fps` (config fps ± 0.01), `video_codec` h264, `audio` (exactly one AAC stream).
- `manifest`: `compliance.render_manifest_problems` ("Ad" label 0.0 → ≥3.0 s, source audio only, size, length).
- `campaign_active`, and per platform `platform:<p>` via `compliance.assert_platform_allowed` (PRD F15, invariant 9).
- per platform `caption:<p>`: present, starts with "#ad ", every campaign `required_tags` present, within the
  platform limit, no URLs on X (invariants 2, 9).
- `youtube_title` when YouTube is a target: present, and "#ad " + title within the 100-character limit (the payload
  builder prefixes "#ad ", publish/upload_post.py).
- Layouts (feat/reframe), when the manifest records a crop plan (label layout 3+): `crop_bounds` (every crop and
  every keyframe of a moving crop inside the source; the facecam box too) and `pan_speed` (largest |dx/dt| between
  keyframes within `max_pan_px_s`). Stacked clips also get `facecam_face`: frames of the *rendered* file's facecam
  panel are checked for a face (`FACECAM_QA_MIN_RATIO`); with no face detector available it fails (never skipped).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from clipforge import compliance
from clipforge.qa.probe import ProbeError, ProbeResult, probe
from clipforge.render.crop_path import max_speed
from clipforge.render.encode import sha256_file
from clipforge.schemas import Campaign, CampaignStatus, CropMode, Platform, QACheck, QAReport, Rect, RenderManifest

DURATION_TOLERANCE_S = 0.5
FPS_TOLERANCE = 0.01
PAN_TOLERANCE_PX_S = 1e-6
FACECAM_QA_FRAMES = 6
FACECAM_QA_MIN_RATIO = 0.5

# (rendered clip, facecam panel rect, clip duration) -> share of sampled panel frames with a face, None = no detector
FaceCheckFn = Callable[[Path, Rect, float], float | None]


def _check(name: str, problems: Sequence[str]) -> QACheck:
    return QACheck(name=name, passed=not problems, detail="; ".join(problems))


def duration_check(duration_s: float) -> QACheck:
    """Invariant 10: clips are 10-90 s."""
    return _check("duration", compliance.duration_problems(duration_s))


def duration_matches_manifest(p: ProbeResult, manifest: RenderManifest) -> QACheck:
    diff = abs(p.duration_s - manifest.duration_s)
    problems = (
        [f"file is {p.duration_s:.2f}s, manifest says {manifest.duration_s:.2f}s"]
        if diff > DURATION_TOLERANCE_S
        else []
    )
    return _check("duration_matches_manifest", problems)


def resolution_check(p: ProbeResult) -> QACheck:
    want = (compliance.RENDER_WIDTH, compliance.RENDER_HEIGHT)
    problems = [] if (p.width, p.height) == want else [f"resolution {p.width}x{p.height}, expected {want[0]}x{want[1]}"]
    return _check("resolution", problems)


def fps_check(p: ProbeResult, fps: float) -> QACheck:
    ok = p.fps is not None and abs(p.fps - fps) <= FPS_TOLERANCE
    return _check("fps", [] if ok else [f"fps {p.fps}, expected {fps:g}"])


def video_codec_check(p: ProbeResult) -> QACheck:
    problems = (
        []
        if p.video_streams == 1 and p.video_codec == "h264"
        else [f"{p.video_streams} video stream(s), codec {p.video_codec}; expected one h264"]
    )
    return _check("video_codec", problems)


def audio_check(p: ProbeResult) -> QACheck:
    problems = (
        []
        if p.audio_streams == 1 and p.audio_codec == "aac"
        else [f"{p.audio_streams} audio stream(s), codec {p.audio_codec}; expected one aac"]
    )
    return _check("audio", problems)


def manifest_check(manifest: RenderManifest) -> QACheck:
    """Invariants 3 and 10 from the manifest."""
    return _check("manifest", compliance.render_manifest_problems(manifest))


def campaign_active_check(campaign: Campaign) -> QACheck:
    problems = [] if campaign.status is CampaignStatus.ACTIVE else [f"campaign is {campaign.status.value}"]
    return _check("campaign_active", problems)


def platform_check(campaign: Campaign, platform: Platform, enabled: Iterable[Platform] | None) -> QACheck:
    try:
        compliance.assert_platform_allowed(campaign, platform, enabled)
    except compliance.ComplianceError as exc:
        return _check(f"platform:{platform.value}", [str(exc)])
    return _check(f"platform:{platform.value}", [])


has_tag = compliance.has_tag  # one rule for normalize_caption and QA (Phase 2: was a copy of the regex)


def youtube_title_check(title: str | None) -> QACheck:
    text = (title or "").strip()
    if not text:
        return _check("youtube_title", ["no YouTube title"])
    full = f"{compliance.AD_PREFIX} {text}"
    if len(full) > compliance.YOUTUBE_TITLE_LIMIT:
        return _check(
            "youtube_title", [f"title is {len(full)} chars with '#ad ', limit {compliance.YOUTUBE_TITLE_LIMIT}"]
        )
    return _check("youtube_title", [])


def caption_check(platform: Platform, caption: str | None, required_tags: Iterable[str] = ()) -> QACheck:
    name = f"caption:{platform.value}"
    if caption is None:
        return _check(name, ["no caption for this platform"])
    problems: list[str] = []
    if not caption.startswith(compliance.AD_PREFIX + " "):
        problems.append("caption does not start with '#ad '")
    if not caption[len(compliance.AD_PREFIX) :].strip():
        problems.append("caption has nothing after '#ad'")
    missing = [t.strip() for t in required_tags if t.strip() and not has_tag(caption, t.strip())]
    if missing:
        problems.append(f"missing required tags: {', '.join(missing)}")
    length, limit = compliance.caption_length(caption, platform), compliance.CAPTION_LIMITS[platform]
    if length > limit:
        problems.append(f"caption is {length} long, {platform.value} limit is {limit}")
    if platform is Platform.X and compliance.contains_url(caption):
        problems.append("X caption contains a URL")
    return _check(name, problems)


def crop_bounds_check(manifest: RenderManifest) -> QACheck:
    plan = manifest.crop_plan
    problems: list[str] = []
    if plan is None:
        return _check("crop_bounds", ["no crop plan recorded"])
    for i, c in enumerate(plan.shots):
        if c.mode is CropMode.FIT_BLUR:
            continue
        if c.x + c.w > plan.src_w or c.y + c.h > plan.src_h:
            problems.append(f"shot {i}: crop x {c.x} y {c.y} {c.w}x{c.h} runs outside {plan.src_w}x{plan.src_h}")
        bad = [k.x for k in c.keyframes if k.x + c.w > plan.src_w]
        if bad:
            problems.append(f"shot {i}: moving crop leaves the frame (left edge {max(bad)} + {c.w} > {plan.src_w})")
        f = c.facecam
        if c.mode is CropMode.STACKED and (f is None or f.x + f.w > plan.src_w or f.y + f.h > plan.src_h):
            problems.append(f"shot {i}: facecam box missing or outside the source")
    return _check("crop_bounds", problems)


def pan_speed_check(manifest: RenderManifest) -> QACheck:
    plan = manifest.crop_plan
    if plan is None:
        return _check("pan_speed", ["no crop plan recorded"])
    moving = [c for c in plan.shots if c.keyframes]
    if not moving:
        return _check("pan_speed", [])
    limit = manifest.max_pan_px_s
    if limit is None:
        return _check("pan_speed", ["moving crop without a recorded pan speed limit"])
    problems = []
    for i, c in enumerate(plan.shots):
        speed = max_speed(c.keyframes)
        if speed > limit + PAN_TOLERANCE_PX_S:
            problems.append(f"shot {i}: pans at {speed:.0f} px/s, limit {limit:.0f} px/s")
    return _check("pan_speed", problems)


def facecam_face_check(manifest: RenderManifest, ratio: float | None) -> QACheck:
    if ratio is None:
        return _check("facecam_face", ["no face detector available to check the facecam panel"])
    if ratio < FACECAM_QA_MIN_RATIO:
        return _check(
            "facecam_face",
            [f"a face is in the facecam panel in {ratio:.0%} of frames, needs {FACECAM_QA_MIN_RATIO:.0%}"],
        )
    return _check("facecam_face", [])


def layout_checks(manifest: RenderManifest, face_ratio: float | None = None) -> list[QACheck]:
    """Crop and facecam checks for label-layout-3 renders (older manifests have no crop plan to check)."""
    if manifest.crop_plan is None and manifest.ad_label_layout < 3:
        return []
    checks = [crop_bounds_check(manifest), pan_speed_check(manifest)]
    if manifest.layout == "stacked":
        checks.append(facecam_face_check(manifest, face_ratio))
    return checks


def default_face_check(clip: Path, panel: Rect, duration_s: float) -> float | None:
    """Share of FACECAM_QA_FRAMES frames of the rendered clip's facecam panel with a face (MediaPipe)."""
    try:
        from clipforge.render.face_detect import MediaPipeImageDetector
    except ImportError:
        return None
    from clipforge.render.facecam import MIN_SCORE, PRESENCE_MIN_FACE_H, grab_frame
    from clipforge.schemas import PxBox

    box = PxBox(x=panel.x0, y=panel.y0, w=panel.x1 - panel.x0, h=panel.y1 - panel.y0)
    try:
        detector = MediaPipeImageDetector()
    except ImportError:
        return None
    hits = 0
    with detector:
        for i in range(FACECAM_QA_FRAMES):
            t = duration_s * (i + 0.5) / FACECAM_QA_FRAMES
            rgb = grab_frame(clip, t, compliance.RENDER_WIDTH, compliance.RENDER_HEIGHT, crop=box)
            if rgb is None:
                continue
            faces = [f for f in detector(rgb) if f.score >= MIN_SCORE and f.h >= PRESENCE_MIN_FACE_H * box.h]
            hits += bool(faces)
    return hits / FACECAM_QA_FRAMES


def run_qa(
    manifest: RenderManifest,
    p: ProbeResult,
    campaign: Campaign,
    platforms: Sequence[Platform],
    captions: Mapping[Platform, str],
    *,
    fps: float = 30.0,
    enabled: Iterable[Platform] | None = None,
    file_check: QACheck | None = None,
    youtube_title: str | None = None,
    face_ratio: float | None = None,
) -> QAReport:
    """Pure QA over already-gathered facts. `platforms` are the platforms this clip is meant for. `face_ratio`: share
    of the rendered facecam panel's sampled frames with a face (stacked clips only; None = not measured)."""
    enabled_list = list(enabled) if enabled is not None else None
    checks: list[QACheck] = [file_check] if file_check is not None else []
    checks += [
        duration_check(p.duration_s),
        duration_matches_manifest(p, manifest),
        resolution_check(p),
        fps_check(p, fps),
        video_codec_check(p),
        audio_check(p),
        manifest_check(manifest),
        campaign_active_check(campaign),
        *layout_checks(manifest, face_ratio),
    ]
    if not platforms:
        checks.append(_check("platforms", ["no target platforms"]))
    for platform in platforms:
        checks.append(platform_check(campaign, platform, enabled_list))
        checks.append(caption_check(platform, captions.get(platform), campaign.required_tags))
        if platform is Platform.YOUTUBE:
            checks.append(youtube_title_check(youtube_title))
    return QAReport(clip_path=manifest.clip_path, checks=checks)


def file_integrity_check(manifest: RenderManifest) -> QACheck:
    path = Path(manifest.clip_path)
    if not path.is_file():
        return _check("file", [f"clip file missing: {path}"])
    digest = sha256_file(path)
    return _check("file", [] if digest == manifest.sha256 else ["clip file SHA-256 differs from the manifest"])


def qa_clip(
    manifest: RenderManifest,
    campaign: Campaign,
    platforms: Sequence[Platform],
    captions: Mapping[Platform, str],
    *,
    fps: float = 30.0,
    enabled: Iterable[Platform] | None = None,
    probe_fn: Callable[[Path], ProbeResult] = probe,
    youtube_title: str | None = None,
    face_check_fn: FaceCheckFn = default_face_check,
) -> QAReport:
    """QA a rendered file on disk: integrity, ffprobe, then `run_qa`. A probe failure is a failed report."""
    file_check = file_integrity_check(manifest)
    if not file_check.passed:
        return QAReport(clip_path=manifest.clip_path, checks=[file_check])
    try:
        p = probe_fn(Path(manifest.clip_path))
    except ProbeError as exc:
        return QAReport(clip_path=manifest.clip_path, checks=[file_check, _check("probe", [str(exc)])])
    face_ratio = None
    if manifest.layout == "stacked" and manifest.facecam_rect is not None:
        face_ratio = face_check_fn(Path(manifest.clip_path), manifest.facecam_rect, p.duration_s)
    return run_qa(
        manifest,
        p,
        campaign,
        platforms,
        captions,
        fps=fps,
        enabled=enabled,
        file_check=file_check,
        youtube_title=youtube_title,
        face_ratio=face_ratio,
    )
