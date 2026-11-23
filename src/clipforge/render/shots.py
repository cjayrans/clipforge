"""Shot boundaries inside a candidate range (ARCHITECTURE §4.4).

`detect_shots` runs PySceneDetect's `ContentDetector` over [start_s, end_s] of a source file; `normalize_shots` is the
pure core that turns raw scene boundaries into a contiguous shot list covering exactly the range.

Verified against the installed scenedetect 0.7.3 source: `scenedetect.open_video(path)` returns a `VideoStream`
with `seek(seconds)`; `SceneManager.add_detector`, `detect_scenes(video, end_time=...)` and `get_scene_list()`
(pairs of `FrameTimecode`, each with a `.seconds` property); `ContentDetector(threshold=27.0, min_scene_len=15)`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_THRESHOLD = 27.0
DEFAULT_MIN_SHOT_S = 1.0


@dataclass(frozen=True)
class Shot:
    """One shot, in source seconds."""

    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def normalize_shots(
    boundaries: Iterable[tuple[float, float]], start_s: float, end_s: float, min_shot_s: float = DEFAULT_MIN_SHOT_S
) -> list[Shot]:
    """Contiguous shots covering exactly [start_s, end_s].

    Raw scene boundaries are clipped to the range and only their cut points are kept. Shots shorter than
    `min_shot_s` are merged into the previous shot (or the next one, for the first shot). With no usable cuts the
    whole range is one shot.
    """
    if end_s <= start_s:
        raise ValueError(f"empty range {start_s}-{end_s}")
    rounded = {round(t, 3) for a, b in boundaries for t in (a, b)}
    cuts = sorted(t for t in rounded if start_s < t < end_s)
    edges = [start_s, *cuts, end_s]
    shots = [Shot(a, b) for a, b in zip(edges, edges[1:], strict=False) if b > a]

    merged: list[Shot] = []
    for shot in shots:
        if merged and shot.duration_s < min_shot_s:
            merged[-1] = Shot(merged[-1].start_s, shot.end_s)
        else:
            merged.append(shot)
    # A short first shot has no predecessor: fold it into the next one.
    if len(merged) > 1 and merged[0].duration_s < min_shot_s:
        merged[1] = Shot(merged[0].start_s, merged[1].end_s)
        merged.pop(0)
    return merged


def detect_shots(
    path: Path | str,
    start_s: float,
    end_s: float,
    threshold: float = DEFAULT_THRESHOLD,
    min_shot_s: float = DEFAULT_MIN_SHOT_S,
) -> list[Shot]:
    """Shot list for [start_s, end_s] of `path` using PySceneDetect's ContentDetector."""
    from scenedetect import ContentDetector, SceneManager, open_video

    video = open_video(str(path))
    fps = float(video.frame_rate)
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold, min_scene_len=max(1, round(min_shot_s * fps))))
    video.seek(start_s)
    manager.detect_scenes(video, end_time=end_s)
    scenes = manager.get_scene_list()
    boundaries = [(a.seconds, b.seconds) for a, b in scenes]
    return normalize_shots(boundaries, start_s, end_s, min_shot_s)
