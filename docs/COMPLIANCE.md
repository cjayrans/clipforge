# Compliance invariants

These rules keep the accounts and the Content Rewards account in good standing. Each one is enforced by a function in
`src/clipforge/compliance.py` (or the module named) and proven by a test in `tests/compliance/`. A change that
weakens any of them needs Carson's explicit approval in the PR description.

Sources: Content Rewards Creator Terms (https://contentrewards.com/terms), Content Rewards FTC page
(https://contentrewards.com/ftc-compliance), FTC Endorsement Guides FAQ
(https://www.ftc.gov/business-guidance/resources/ftcs-endorsement-guides-what-people-are-asking), platform
originality and multi-account rules summarised in the operator runbook.

| # | Invariant | Enforced in | Test |
| --- | --- | --- | --- |
| 1 | A source can be downloaded only if its URL is registered on an active campaign. Twitch downloads need `allow_stream_download=true` on that campaign; YouTube and all other hosts are never downloaded. | `compliance.assert_registered_source`, `ingest.download` | `test_unregistered_source_refused`, `test_youtube_host_refused`, `test_twitch_requires_campaign_permission` |
| 2 | Every caption on every platform starts with `#ad` (exact, then a space). | `compliance.normalize_caption`, `publish.upload_post.build_payload` | `test_caption_starts_with_ad_all_platforms` |
| 3 | Every rendered clip has the "Ad" label from 0.0 s to ≥ 3.0 s, recorded in the manifest and checked by QA. | `render.overlays`, `compliance.assert_render_manifest` | `test_ad_label_first_three_seconds` |
| 4 | Paid-content flags are always set: TikTok `brand_content_toggle=true`; YouTube `hasPaidProductPlacement=true`; AI/synthetic flags false. No code path generates synthetic faces or voices. | `publish.upload_post.build_payload` | `test_disclosure_flags_per_platform` |
| 5 | No post is published without an `approvals` row with decision `approve` by the operator, made after the clip's latest render. | `compliance.assert_human_approved`, `publish.dispatcher` | `test_publish_requires_fresh_human_approval` |
| 6 | No publishing on an evening that is not armed. | `publish.dispatcher` | `test_unarmed_evening_publishes_nothing` |
| 7 | A post maps to exactly one campaign submission; a rejected post is never resubmitted; no clip file is published twice to the same platform. | DB unique constraints + `compliance.assert_unique_post` | `test_no_duplicate_platform_post`, `test_one_submission_per_post` |
| 8 | No engagement automation: the codebase never calls like/follow/comment/DM endpoints and never sends `first_comment`. | `publish.upload_post.build_payload`, static test | `test_no_engagement_fields_or_endpoints` (greps `src/` for forbidden names) |
| 9 | X captions contain no URLs; political campaigns never publish to TikTok or X. | `compliance.normalize_caption`, `compliance.allowed_platforms` | `test_x_caption_has_no_urls`, `test_political_blocked_tiktok_x` |
| 10 | Clips are 10–90 s and contain no added music track. | `qa.checks`, `render.encode` (only source audio is mapped) | `test_duration_bounds`, `test_only_source_audio_mapped` |
| 11 | Publish slots fall inside the operator's configured window. | `publish.slots`, `config` validation | `test_slots_inside_operator_window` |
| 12 | API spend stops at the monthly cap. | `costs.guard` | `test_spend_cap_pauses_pipeline` |

Also enforced (no separate number): tests never run with `PUBLISH_MODE=live`
(`tests/conftest.py` fails the session if it is set), and the bot ignores non-owner chats
(`test_bot_ignores_other_chats`).
