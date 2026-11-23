"""QA gate: ffprobe + render manifest + captions + campaign → `schemas.QAReport` (ARCHITECTURE §4.5, PRD F6).

Failures never reach review: Phase 2 moves a clip to QA_FAILED unless `report.passed`.

Checks (one `QACheck` each, all run so the report lists every problem):
- `file`: the clip exists and its SHA-256 equals the manifest's (no stale or swapped file).
- `duration` (10–90 s, invariant 10) on the probed duration, and `duration_matches_manifest` (±0.5 s).
- `resolution` 1080×1920, `fps` (config fps ± 0.01), `video_codec` h264, `audio` (exactly one AAC stream).
- `manifest`: `compliance.render_manifest_problems` ("Ad" label 0.0 → ≥3.0 s, source audio only, size, length).
- `campaign_active`, and per platform `platform:<p>` via `compliance.assert_platform_allowed` (PRD F15, invariant 9).
- per platform `caption:<p>`: present, starts with "#ad ", within the platform limit, no URLs on X (invariants 2, 9).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path

from clipforge import compliance
from clipforge.qa.probe import ProbeError, ProbeResult, probe
from clipforge.render.encode import sha256_file
from clipforge.schemas import Campaign, CampaignStatus, Platform, QACheck, QAReport, RenderManifest

DURATION_TOLERANCE_S = 0.5
FPS_TOLERANCE = 0.01


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


def caption_check(platform: Platform, caption: str | None) -> QACheck:
    name = f"caption:{platform.value}"
    if caption is None:
        return _check(name, ["no caption for this platform"])
    problems: list[str] = []
    if not caption.startswith(compliance.AD_PREFIX + " "):
        problems.append("caption does not start with '#ad '")
    if not caption[len(compliance.AD_PREFIX) :].strip():
        problems.append("caption has nothing after '#ad'")
    length, limit = compliance.caption_length(caption, platform), compliance.CAPTION_LIMITS[platform]
    if length > limit:
        problems.append(f"caption is {length} long, {platform.value} limit is {limit}")
    if platform is Platform.X and compliance.contains_url(caption):
        problems.append("X caption contains a URL")
    return _check(name, problems)


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
) -> QAReport:
    """Pure QA over already-gathered facts. `platforms` are the platforms this clip is meant for."""
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
    ]
    if not platforms:
        checks.append(_check("platforms", ["no target platforms"]))
    for platform in platforms:
        checks.append(platform_check(campaign, platform, enabled_list))
        checks.append(caption_check(platform, captions.get(platform)))
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
) -> QAReport:
    """QA a rendered file on disk: integrity, ffprobe, then `run_qa`. A probe failure is a failed report."""
    file_check = file_integrity_check(manifest)
    if not file_check.passed:
        return QAReport(clip_path=manifest.clip_path, checks=[file_check])
    try:
        p = probe_fn(Path(manifest.clip_path))
    except ProbeError as exc:
        return QAReport(clip_path=manifest.clip_path, checks=[file_check, _check("probe", [str(exc)])])
    return run_qa(manifest, p, campaign, platforms, captions, fps=fps, enabled=enabled, file_check=file_check)
