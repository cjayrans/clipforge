"""QA gate: ffprobe + render manifest -> `schemas.QAReport`. Failures never reach review.

Owner: Phase 1 worktree C (`feat/render`). See docs/ARCHITECTURE.md §4.5.
Must include `compliance.render_manifest_problems` and `compliance.assert_platform_allowed`.
Expected entry point: `qa.checks.duration_check(duration_s) -> schemas.QACheck`.
"""
