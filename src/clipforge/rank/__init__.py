"""Candidate windows, Haiku scoring and Sonnet copy.

Owner: Phase 1 worktree B (`feat/rank`). See docs/ARCHITECTURE.md §4.3.
Captions must go through `compliance.normalize_caption`.

Entry points:
- `rank.copy.rank_source(transcript, campaign, platforms, client=ClaudeClient)` → `RankResult` (candidates with
  copy, score stats, failures). Persists nothing but cost rows.
- `rank.copy.write_copy(candidate, clip_text, campaign, platforms, client=...)` → `schemas.ClipCopy` (bot "New hook").
- `rank.windows.build_windows`, `rank.select.select_candidates`: pure.
- `rank.claude_client.ClaudeClient.from_settings(settings, session)`: the one Anthropic client.
"""
