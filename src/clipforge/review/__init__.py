"""Telegram bot: previews, approvals, arming, submission marking, reconcile.

Owner: Phase 1 worktree D (`feat/ops`). See docs/ARCHITECTURE.md §4.6.

Entry points other code already expects (keep these names):
- `review.bot.run_bot(settings) -> None`                          (`cf bot`)
- `review.bot.is_owner_chat(chat_id, owner_chat_id) -> bool`     (every handler checks it first)

Modules:
- `bot` handlers, owner gate and background tasks; `keyboards` callback data; `service` approve/reject/skip (DB only)
- `evening` arm message, arming, Submit-now, reminders, Submitted/Missed
- `previews` review videos; `deliver` alert delivery + worker watchdog; `alerts` how any module raises an operator alert
  (an `events` row with entity "alert"); `notify` the one Telegram sender (bot and worker); `views` message text
- Phase 2 wires the "New hook" / "Shorter" buttons by passing `service.ReviewHooks` to `bot.run_bot`.
"""
