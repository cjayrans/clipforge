"""Submissions, reconcile, YouTube views, weekly learning and reports.

Owner: Phase 1 worktree D (`feat/ops`). See docs/ARCHITECTURE.md §4.8.

Entry points other code already expects (keep these names):
- `track.reports.weekly_report(session, settings) -> pathlib.Path`        (`cf report weekly`)
- `track.reconcile.export_csv(session, settings, path) -> int`            (`cf export-reconcile`)
- `track.reconcile.import_csv(session, settings, path) -> int`            (`cf import-reconcile`)
"""
