"""Nightly orchestration: ingest -> transcribe -> rank -> render -> QA -> review.

Owner: Phase 2 (P2.2), on `main`. Expected entry point: `run_once(settings) -> None` (`cf run --once`), which calls
`costs.guard()` before each paid stage and `costs.pause()` on `SpendCapReached`.
"""
