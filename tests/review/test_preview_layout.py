"""The review preview says which layout a clip was rendered with (feat/reframe)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from clipforge.config import Settings
from clipforge.review.views import preview_caption
from tests.compliance._helpers import manifest
from tests.support.factories import Factory


def test_preview_names_the_layout_and_render_time(factory: Factory, session: Session, settings: Settings) -> None:
    clip = factory.clip(layout="stacked", render_s=41.6)
    text = preview_caption(session, settings, clip)
    assert "Layout: stacked (facecam top, gameplay below) | render 42s" in text


def test_preview_says_why_auto_skipped_a_layout(
    factory: Factory, session: Session, settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "clip_1.mp4"
    m = manifest(
        clip_path=str(path),
        layout="crop",
        layout_reason="stacked: a face is in the facecam box in 20% of 60 sampled frames, needs 60%; "
        "shot 20.0s stationary (salient motion stays in the centre crop)",
    )
    path.with_suffix(".manifest.json").write_text(m.model_dump_json())
    clip = factory.clip(path=str(path), layout="crop", render_s=30.0)
    text = preview_caption(session, settings, clip)
    assert "Layout: crop (9:16 gameplay crop) | render 30s" in text
    assert "Not used: stacked: a face is in the facecam box in 20%" in text
    assert "shot 20.0s" not in text  # per-shot camera notes stay in the manifest


def test_clips_rendered_before_layouts_read_as_fit(factory: Factory, session: Session, settings: Settings) -> None:
    text = preview_caption(session, settings, factory.clip())
    assert "Layout: fit (whole frame over blur)" in text
    assert "render" not in text.split("Layout:")[1].splitlines()[0]
