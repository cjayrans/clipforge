"""APScheduler worker: every job in docs/ARCHITECTURE.md §4.9.

Owner: Phase 1 worktree D (`feat/ops`).
Entry point other code already expects: `scheduler.worker.run_worker(settings) -> None` (`cf worker`).
The worker must call `heartbeat.beat(settings.heartbeat_dir, "worker")` every 60 s (job `heartbeat`).
"""
