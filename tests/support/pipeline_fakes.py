"""Fakes for the pipeline tests: media tools and render (no ffmpeg), plus helpers for mocked Groq / Anthropic HTTP.

External HTTP is mocked at the wire: respx for httpx (Groq SDK, direct downloads, Upload-Post) and
`httpx2.MockTransport` for the Anthropic SDK (tests/rank/anthropic_mock.py). Nothing touches the network.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx2

from clipforge.qa.probe import ProbeResult
from clipforge.render.reframe import FaceBox
from clipforge.render.shots import Shot
from tests.rank.anthropic_mock import FakeAnthropic, json_text
from tests.rank.moment_fakes import moment_tools
from tests.rank.test_score import echo_scores

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
SOURCE_URL = "https://cdn.example.com/gta6/ep1.mp4"
SOURCE_DURATION_S = 90.0

SRC_PROBE = ProbeResult(
    duration_s=SOURCE_DURATION_S, size_bytes=1, width=1920, height=1080, fps=30.0, video_codec="h264",
    audio_codec="aac", audio_streams=1, video_streams=1,
)  # fmt: skip


def groq_verbose_json() -> dict[str, Any]:
    """The talk fixture transcript as Groq would return it (verbose_json, word + segment timestamps)."""
    data = json.loads((FIXTURES / "transcript_talk.json").read_text())
    return {
        "text": " ".join(w["text"] for w in data["words"]),
        "language": "en",
        "duration": data["duration_s"],
        "words": [{"word": w["text"], "start": w["start_s"], "end": w["end_s"]} for w in data["words"]],
        "segments": [
            {"id": i, "start": s["start_s"], "end": s["end_s"], "text": s["text"]}
            for i, s in enumerate(data["segments"])
        ],
        "x_groq": {"id": "req_test"},
    }


def media_runner(calls: list[list[str]] | None = None) -> Any:
    """Fake ffmpeg (writes its output file) and ffprobe (a 90 s file with video and audio) for ingest/transcribe."""

    def run(args: Sequence[str]) -> str:
        if calls is not None:
            calls.append(list(args))
        if args[0] == "ffmpeg":
            Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[-1]).write_bytes(b"ID3 fake audio")
            return ""
        return json.dumps(
            {
                "format": {"duration": str(SOURCE_DURATION_S)},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        )

    return run


class FakeRender:
    """Stands in for ffmpeg/ffprobe/PySceneDetect/MediaPipe inside the real `render_candidate` and `qa_clip`.

    The fake encoder writes `fake mp4 <duration>` (the `-t` value) to the output path; the fake probe reads it back, so
    the manifest, the file hash and QA all agree like a real render. `fail` makes the next N encodes raise."""

    def __init__(self, *, fail: int = 0, out_fps: float = 30.0) -> None:
        self.encodes: list[list[str]] = []
        self.fail = fail
        self.out_fps = out_fps
        from clipforge.render.facecam import Presence

        self.presence_result: Any = Presence(ratio=0.0, samples=0, problem="fake: no face")
        self.facecam_calls: list[Path] = []
        self.facecam_windows: list[list[tuple[float, float]]] = []
        self.facecam_result: Any = None  # render.facecam.FacecamDetection or None (detection could not run)
        self.face_ratio: float | None = None

    def facecam(self, path: Path, windows: list[tuple[float, float]]) -> Any:
        self.facecam_calls.append(path)
        self.facecam_windows.append(windows)
        return self.facecam_result

    def face_check(self, clip: Path, panel: Any, duration: float) -> float | None:
        return self.face_ratio

    def probe(self, path: Path) -> ProbeResult:
        p = Path(path)
        if p.name.startswith("clip_") and p.suffix == ".mp4":
            duration = float(p.read_text().split()[-1])
            return ProbeResult(
                duration_s=duration, size_bytes=p.stat().st_size, width=1080, height=1920, fps=self.out_fps,
                video_codec="h264", audio_codec="aac", audio_streams=1, video_streams=1,
            )  # fmt: skip
        return SRC_PROBE

    def shots(self, path: Path, start: float, end: float) -> list[Shot]:
        return [Shot(start, end)]

    def run(self, args: list[str]) -> None:
        from clipforge.render.encode import RenderError

        if self.fail > 0:
            self.fail -= 1
            raise RenderError("ffmpeg failed (exit 1): fake failure")
        self.encodes.append(args)
        duration = float(args[args.index("-t") + 1])
        Path(args[-1]).write_text(f"fake mp4 {duration}")

    @contextlib.contextmanager
    def detector(self, path: Path) -> Iterator[Any]:
        def detect(shot: Shot) -> list[list[FaceBox]]:
            return [[]]  # no faces: fit_blur

        yield detect

    def motion(self, path: Path, start: float, end: float, w: int, h: int, mask: Any) -> list[Any]:
        return []  # no motion: stationary centre crops

    def presence(self, path: Path, box: Any, start: float, end: float) -> Any:
        return self.presence_result

    def kwargs(self) -> dict[str, Any]:
        return {
            "detector_factory": self.detector,
            "shots_fn": self.shots,
            "probe_fn": self.probe,
            "run": self.run,
            "motion_fn": self.motion,
            "presence_fn": self.presence,
        }


def copy_payload(hook: str = "He tried jumping to a helicopter") -> dict[str, Any]:
    return {
        "hook": hook,
        "youtube_title": "Streamer jumps for a helicopter mid-heist in GTA 6",
        "captions": [
            {"platform": "tiktok", "text": "Clip that, somebody clip that #heist"},
            {"platform": "instagram", "text": "The jump of the century"},
        ],
    }


def fake_anthropic(hooks: Sequence[str] = (), proposals: Sequence[dict[str, Any]] = ()) -> FakeAnthropic:
    """Haiku scoring echoes a score for every window or moment candidate; the moment content pass proposes
    `proposals` (none by default); the keyframe check approves; Sonnet copy returns `copy_payload`, using `hooks` in
    order."""
    queue = list(hooks)
    moments = moment_tools(proposals=proposals)

    def respond(body: dict[str, Any]) -> httpx2.Response:
        if body.get("tools"):
            if body["tools"][0]["name"] in ("score_moments", "propose_moments", "judge_frames"):
                return moments(body)
            return echo_scores(body)
        hook = queue.pop(0) if queue else "He tried jumping to a helicopter"
        return json_text(copy_payload(hook))

    return FakeAnthropic(default=respond)
