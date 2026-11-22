"""Telegram bot: previews, approvals, arming, submission marking, reconcile.

Owner: Phase 1 worktree D (`feat/ops`). See docs/ARCHITECTURE.md §4.6.

Entry points other code already expects (keep these names):
- `review.bot.run_bot(settings) -> None`                          (`cf bot`)
- `review.bot.is_owner_chat(chat_id, owner_chat_id) -> bool`     (every handler checks it first)
"""
