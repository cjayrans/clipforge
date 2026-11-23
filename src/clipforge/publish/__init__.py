"""Slot planning, Upload-Post client and the evening dispatcher.

Owner: Phase 1 worktree D (`feat/ops`). See docs/ARCHITECTURE.md §4.7.

Entry points the compliance tests expect (keep these names):
- `publish.slots.plan_slots(app_config, day) -> list[schemas.Slot]`
- `publish.upload_post.build_payload(request: PublishRequest, campaign: CampaignIn, app_config) -> dict[str, Any]`
- `publish.dispatcher.dispatch_due_slots(session, settings, now) -> list[schemas.PublishResult]`
Every payload goes through `compliance.assert_no_engagement_fields` (no first-comment or other engagement fields).

Also: `publish.dispatcher.poll_pending_posts` (status polling and crash recovery), `publish.slots.assign_clips`,
`publish.queries` (row -> schema helpers shared with review and track).
"""
