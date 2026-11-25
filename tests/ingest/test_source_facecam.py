"""`cf source facecam` (feat/reframe): stored result, re-detection, campaign override, debug images."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.db import EventRow
from clipforge.ingest.registry import RegistryError, source_facecam
from clipforge.qa.probe import ProbeResult
from clipforge.render.facecam import FacecamDetection
from clipforge.schemas import PxBox
from tests.support.factories import Factory

SRC = ProbeResult(
    duration_s=3600.0, size_bytes=1, width=1920, height=1080, fps=30.0, video_codec="h264", audio_codec="aac",
    audio_streams=1, video_streams=1,
)  # fmt: skip
FOUND = FacecamDetection(
    status="found", src_w=1920, src_h=1080, hits=31, samples=40,
    box=PxBox(x=1400, y=700, w=444, h=264), face=PxBox(x=1572, y=764, w=100, h=110),
)  # fmt: skip


def _source(factory: Factory, tmp_path: Path, **campaign: Any) -> Any:
    media = tmp_path / "vod.mp4"
    media.write_bytes(b"x")
    return factory.source(factory.campaign(content_kind="game", **campaign), local_path=str(media))


def _grab(path: Path, t: float, w: int, h: int) -> Any:
    return np.zeros((h, w, 3), dtype=np.uint8)


def test_detects_once_stores_and_audits(factory: Factory, session: Session, tmp_path: Path) -> None:
    src = _source(factory, tmp_path)
    calls: list[Path] = []

    def detect(path: Path) -> FacecamDetection:
        calls.append(path)
        return FOUND

    assert source_facecam(session, None, src.id, detect_fn=detect, probe_fn=lambda p: SRC) == FOUND
    assert source_facecam(session, None, src.id, detect_fn=detect, probe_fn=lambda p: SRC) == FOUND
    assert len(calls) == 1  # stored after the first run
    source_facecam(session, None, src.id, redetect=True, detect_fn=detect, probe_fn=lambda p: SRC)
    assert len(calls) == 2
    events = session.scalars(select(EventRow).where(EventRow.entity == "source", EventRow.actor == "operator")).all()
    assert len(events) == 2
    assert src.facecam_json["box"] == {"x": 1400, "y": 700, "w": 444, "h": 264}


def test_detection_that_cannot_run_is_an_error(factory: Factory, session: Session, tmp_path: Path) -> None:
    src = _source(factory, tmp_path)
    with pytest.raises(RegistryError, match="could not run"):
        source_facecam(session, None, src.id, detect_fn=lambda p: None, probe_fn=lambda p: SRC)
    assert src.facecam_json is None


def test_debug_images(factory: Factory, session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _source(factory, tmp_path)
    out = tmp_path / "out" / "facecam.jpg"
    out.parent.mkdir()
    source_facecam(
        session, None, src.id, debug_image=out, detect_fn=lambda p: FOUND, probe_fn=lambda p: SRC, grab=_grab
    )
    assert out.is_file()
    assert (out.parent / "facecam_stacked.jpg").is_file()
    printed = " ".join(capsys.readouterr().out.split())  # rich wraps long lines
    assert "facecam x 1400, y 700, 444x264 px of 1920x1080 (face in 31 of 40 sampled frames)" in printed


def test_campaign_box_wins_over_detection(factory: Factory, session: Session, tmp_path: Path) -> None:
    box = {"x_pct": 75, "y_pct": 65, "w_pct": 23, "h_pct": 24}
    src = _source(factory, tmp_path, facecam_box=box)

    def detect(path: Path) -> FacecamDetection:
        raise AssertionError("detection must not run when the campaign sets facecam_box")

    assert source_facecam(session, None, src.id, detect_fn=detect, probe_fn=lambda p: SRC) is None


def test_missing_media_is_refused(factory: Factory, session: Session) -> None:
    src = factory.source(factory.campaign(content_kind="game"), local_path="/nope/vod.mp4")
    with pytest.raises(RegistryError, match="not on disk"):
        source_facecam(session, None, src.id, detect_fn=lambda p: FOUND, probe_fn=lambda p: SRC)
