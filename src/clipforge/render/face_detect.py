"""MediaPipe Tasks FaceDetector adapter → `reframe.FaceBox` frames.

`mediapipe` is installed on Linux only, so it is imported lazily inside `MediaPipeFaceDetector`.

Verified 2026-11-22:
- API (https://ai.google.dev/edge/mediapipe/solutions/vision/face_detector/python, redirects to
  developers.google.com): `mp.tasks.BaseOptions(model_asset_path=...)`, `mp.tasks.vision.FaceDetectorOptions(
  base_options=..., running_mode=mp.tasks.vision.RunningMode.IMAGE, min_detection_confidence=0.5)`,
  `FaceDetector.create_from_options(options)` (context manager), `detector.detect(mp.Image(
  image_format=mp.ImageFormat.SRGB, data=rgb))`.
- Result fields (mediapipe/tasks/python/components/containers/{detections,bounding_box,category}.py on GitHub):
  `result.detections[i].bounding_box.origin_x/origin_y/width/height` (pixels), `.categories[0].score`.
- Model: blaze_face_short_range.tflite, Apache-2.0, 229,746 bytes, SHA-256 b4578f35…152f, from
  https://huggingface.co/litert-community/MediaPipe-FaceDetector-BlazeFace-Short-Range (the URL in the docs' Models
  section; byte-identical to Google's storage.googleapis.com artifact per the model card).
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

from clipforge.render.assets import face_model_path
from clipforge.render.reframe import FaceBox, FrameFaces
from clipforge.render.shots import Shot

SAMPLE_FPS = 5.0


class MediaPipeFaceDetector:
    """Samples a shot at `sample_fps` and returns the faces found in each sampled frame."""

    def __init__(
        self,
        video_path: Path | str,
        *,
        model_path: Path | None = None,
        sample_fps: float = SAMPLE_FPS,
        min_confidence: float = 0.5,
    ) -> None:
        import mediapipe as mp

        options = mp.tasks.vision.FaceDetectorOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path or face_model_path())),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            min_detection_confidence=min_confidence,
        )
        self._mp = mp
        self._detector: Any = mp.tasks.vision.FaceDetector.create_from_options(options)
        self._video_path = str(video_path)
        self._sample_fps = sample_fps

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        self._detector.close()

    def __call__(self, shot: Shot) -> list[FrameFaces]:
        import cv2

        cap = cv2.VideoCapture(self._video_path)
        try:
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            step = max(1, round(fps / self._sample_fps))
            cap.set(cv2.CAP_PROP_POS_MSEC, shot.start_s * 1000.0)
            n_frames = max(1, round(shot.duration_s * fps))
            frames: list[FrameFaces] = []
            for i in range(n_frames):
                ok, bgr = cap.read()
                if not ok:
                    break
                if i % step:
                    continue
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
                result = self._detector.detect(image)
                frames.append([_to_box(d) for d in result.detections])
            return frames
        finally:
            cap.release()


class MediaPipeImageDetector:
    """Faces in one RGB image (numpy HxWx3 uint8), in that image's pixels. Used for facecam detection (on tiles of
    a frame) and for the facecam presence checks (render, QA)."""

    def __init__(self, *, model_path: Path | None = None, min_confidence: float = 0.5) -> None:
        import mediapipe as mp

        options = mp.tasks.vision.FaceDetectorOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=str(model_path or face_model_path())),
            running_mode=mp.tasks.vision.RunningMode.IMAGE,
            min_detection_confidence=min_confidence,
        )
        self._mp = mp
        self._detector: Any = mp.tasks.vision.FaceDetector.create_from_options(options)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._detector.close()

    def __call__(self, rgb: Any) -> list[FaceBox]:
        import numpy as np

        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        return [_to_box(d) for d in self._detector.detect(image).detections]


def _to_box(detection: Any) -> FaceBox:
    bb = detection.bounding_box
    score = detection.categories[0].score if detection.categories else None
    return FaceBox(
        x=float(bb.origin_x), y=float(bb.origin_y), w=float(bb.width), h=float(bb.height), score=float(score or 0.0)
    )
