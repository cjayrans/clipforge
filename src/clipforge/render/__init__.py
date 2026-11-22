"""Shot detection, reframing, captions, overlays and encoding.

Owner: Phase 1 worktree C (`feat/render`). See docs/ARCHITECTURE.md §4.4.
Import `mediapipe` lazily (it is installed on Linux only; tests use `pytest.importorskip`).

Entry points the compliance tests expect (keep these names):
- `render.overlays.ad_label_event(render_config) -> object with start_s, end_s, text`
- `render.encode.build_ffmpeg_args(...) -> list[str]` (maps only the source audio, `0:a`)
"""
