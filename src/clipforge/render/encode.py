"""Encoder: crop plan + ASS → one ffmpeg run → 1080×1920 H.264/AAC clip + `schemas.RenderManifest`.

ARCHITECTURE §4.4: a single filtergraph with `trim` + crop/fit_blur per shot, `concat`, then `ass` (captions, hook,
"Ad" label), `fps`, `format=yuv420p`; `libx264 -preset veryfast -crf 21 -pix_fmt yuv420p -r 30`,
`aac -b:a 128k`, `loudnorm=I=-14:TP=-1.5:LRA=11`.

Invariant 10: exactly one input (the source) and only its audio is mapped (`-map 0:a:0`, filtered with `-af`, never
through a filtergraph label, never mixed). `maps_only_source_audio` checks the final argument list and its result is
what the manifest records as `audio_source_only`.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from clipforge import compliance
from clipforge.config import RenderConfig
from clipforge.schemas import CropMode, CropPlan, RenderManifest, ShotCrop

OUT_W, OUT_H = compliance.RENDER_WIDTH, compliance.RENDER_HEIGHT
BLUR_W, BLUR_H = 270, 480  # background is blurred at quarter size, then scaled up (cheap on CPU)
FFMPEG_TIMEOUT_S = 1800
_UNSAFE_FILTER_CHARS = set(":\\',;[]=\n")
_FORBIDDEN_FLAGS = frozenset({"-filter_complex_script", "-filter_script", "-stream_loop", "-lavfi_script"})
_FILTER_FLAGS = frozenset({"-filter_complex", "-lavfi", "-vf", "-filter:v", "-af", "-filter:a"})
# Audio pads ([0:a], [1:a:0]) or filters that read files or generate/mix audio.
# File-path option values (ass/subtitles filename, fontsdir) are removed before scanning so a folder name like
# "movie-night" cannot trip the check; paths never carry audio into the output.
_PATH_OPTION_RE = re.compile(r"\b(?:filename|fontsdir)=[^:,;\[\]]*")
_AUDIO_FILTER_RE = re.compile(r"\[\d+:a|\b(?:a?movie|amix|amerge|join|anullsrc|sine|aevalsrc|asplit|anoisesrc)\b")


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class EncodeSettings:
    fps: int = 30
    preset: str = "veryfast"
    crf: int = 21
    loudnorm_i: float = -14.0
    loudnorm_tp: float = -1.5
    loudnorm_lra: float = 11.0
    audio_bitrate: str = "128k"
    audio_rate: int = 48_000

    @classmethod
    def from_config(cls, cfg: RenderConfig) -> EncodeSettings:
        return cls(fps=cfg.fps, preset=cfg.preset, crf=cfg.crf, loudnorm_i=cfg.loudnorm_i)


def filter_path(path: Path | str) -> str:
    """A path usable as a filter option value. Paths we write never need escaping; refuse anything that would."""
    text = str(path)
    bad = sorted(_UNSAFE_FILTER_CHARS & set(text))
    if bad:
        raise RenderError(f"path has characters ffmpeg filter options would misread {bad}: {text!r}")
    return text


def _shot_filter(crop: ShotCrop, label_in: str, label_out: str, start: float, end: float, i: int) -> str:
    trim = f"[{label_in}]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS"
    if crop.mode is CropMode.FIT_BLUR:
        return (
            f"{trim},split=2[bg{i}][fg{i}];"
            f"[bg{i}]scale={BLUR_W}:{BLUR_H}:force_original_aspect_ratio=increase,crop={BLUR_W}:{BLUR_H},"
            f"boxblur=10:2,scale={OUT_W}:{OUT_H},setsar=1[bb{i}];"
            f"[fg{i}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[ff{i}];"
            f"[bb{i}][ff{i}]overlay=(W-w)/2:(H-h)/2[{label_out}]"
        )
    return f"{trim},crop={crop.w}:{crop.h}:{crop.x}:0,scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1[{label_out}]"


def default_plan(duration_s: float, clip_start_s: float = 0.0) -> CropPlan:
    """One fit_blur shot over the whole clip (safe default when no plan is given; PRD F16)."""
    shot = ShotCrop(
        start_s=clip_start_s, end_s=clip_start_s + duration_s, mode=CropMode.FIT_BLUR, x=0, w=OUT_W, h=OUT_H
    )
    return CropPlan(src_w=OUT_W, src_h=OUT_H, shots=[shot])


def build_filtergraph(
    plan: CropPlan,
    clip_start_s: float,
    duration_s: float,
    ass_path: Path | str | None,
    fonts_dir: Path | str | None,
    fps: int = 30,
) -> str:
    """Filtergraph for input 0 (already seeked to clip_start_s). Shot times are source seconds."""
    if not plan.shots:
        raise RenderError("crop plan has no shots")
    n = len(plan.shots)
    parts = [f"[0:v]split={n}" + "".join(f"[s{i}]" for i in range(n)) if n > 1 else "[0:v]null[s0]"]
    for i, crop in enumerate(plan.shots):
        start = max(0.0, crop.start_s - clip_start_s)
        end = min(duration_s, crop.end_s - clip_start_s)
        if i == n - 1:
            end = duration_s
        if end <= start:
            raise RenderError(f"shot {i} ({crop.start_s}-{crop.end_s}) is outside the clip")
        parts.append(_shot_filter(crop, f"s{i}", f"v{i}", start, end, i))
    joined = "".join(f"[v{i}]" for i in range(n))
    tail = f"{joined}concat=n={n}:v=1:a=0" if n > 1 else "[v0]null"
    if ass_path is not None:
        tail += f",ass=filename={filter_path(ass_path)}"
        if fonts_dir is not None:
            tail += f":fontsdir={filter_path(fonts_dir)}"
    tail += f",fps={fps},format=yuv420p[v]"
    parts.append(tail)
    return ";".join(parts)


# Pin the sample format and channel layout before the AAC encoder. Without it, Debian bookworm's ffmpeg 5.1 (the
# Docker image) fails with "Cannot select channel layout for the link between filters Parsed_aresample_1 and
# format_out" on loudnorm output; Ubuntu's newer ffmpeg (CI) negotiates it on its own. Mono stays mono.
AUDIO_FORMAT = "aformat=sample_fmts=fltp:channel_layouts=mono|stereo"


def build_ffmpeg_args(
    manifest: RenderManifest,
    *,
    source_path: Path | str = "source.mp4",
    clip_start_s: float = 0.0,
    crop_plan: CropPlan | None = None,
    ass_path: Path | str | None = None,
    fonts_dir: Path | str | None = None,
    settings: EncodeSettings | None = None,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Full ffmpeg argument list producing `manifest.clip_path` from `source_path`.

    The manifest supplies the target (output path, duration, resolution); everything else has safe defaults so the
    audio mapping can be checked from a manifest alone (tests/compliance/test_inv10).
    """
    s = settings or EncodeSettings()
    duration = manifest.duration_s
    compliance.assert_duration_bounds(duration)
    if (manifest.width, manifest.height) != (OUT_W, OUT_H):
        raise RenderError(f"output must be {OUT_W}x{OUT_H}")
    plan = crop_plan or default_plan(duration, clip_start_s)
    graph = build_filtergraph(plan, clip_start_s, duration, ass_path, fonts_dir, s.fps)
    loudnorm = f"loudnorm=I={s.loudnorm_i:g}:TP={s.loudnorm_tp:g}:LRA={s.loudnorm_lra:g}"
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{clip_start_s:.3f}", "-t", f"{duration:.3f}", "-i", str(source_path),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "0:a:0",
        "-c:v", "libx264", "-preset", s.preset, "-crf", str(s.crf), "-pix_fmt", "yuv420p", "-r", str(s.fps),
        "-af", f"{loudnorm},aresample={s.audio_rate},{AUDIO_FORMAT}",
        "-c:a", "aac", "-b:a", s.audio_bitrate, "-ar", str(s.audio_rate),
        "-t", f"{duration:.3f}", "-movflags", "+faststart",
        manifest.clip_path,
    ]  # fmt: skip


def maps_only_source_audio(args: Sequence[str]) -> bool:
    """Invariant 10: the only audio that can reach the output is input 0's own audio track.

    True only when: exactly one `-i`; at least one audio map and every audio map starts with `0:a`; every other map
    is the `[v]` video label or `0:v…`; no filter (`-filter_complex`/`-lavfi`/`-vf`/`-af`) reads an audio pad or
    generates/mixes audio; no filter scripts or input looping (which could hide either).
    """
    if any(a in _FORBIDDEN_FLAGS for a in args):
        return False
    if sum(1 for a in args if a == "-i") != 1:
        return False
    maps = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-map"]
    audio_maps = [m for m in maps if m.startswith("0:a")]
    if not audio_maps or any(m not in ("[v]",) and not m.startswith(("0:a", "0:v")) for m in maps):
        return False
    filters = [args[i + 1] for i, a in enumerate(args[:-1]) if a in _FILTER_FLAGS]
    return not any(_AUDIO_FILTER_RE.search(_PATH_OPTION_RE.sub("", f)) for f in filters)


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run_ffmpeg(args: list[str]) -> None:
    if shutil.which(args[0]) is None:
        raise RenderError(f"{args[0]} not found on PATH")
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    except subprocess.CalledProcessError as exc:
        raise RenderError(f"ffmpeg failed: {exc.stderr.strip()[-1000:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError("ffmpeg timed out") from exc


Runner = Callable[[list[str]], None]
