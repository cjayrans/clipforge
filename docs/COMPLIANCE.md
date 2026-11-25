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
| 1 | A source can be downloaded only if its URL is registered on an active campaign. Twitch downloads need `allow_stream_download=true` on that campaign, and when the campaign lists `stream_channels` a video from any other channel is refused before any media is fetched; YouTube and all other hosts are never downloaded. | `compliance.assert_registered_source`, `ingest.download`, `ingest.twitch` | `test_unregistered_source_refused`, `test_youtube_host_refused`, `test_twitch_requires_campaign_permission`, `test_other_channel_refused_before_any_media` |
| 2 | Every caption on every platform starts with `#ad` (exact, then a space). The campaign's required caption text (`required_tags`) always comes last, after the copy. | `compliance.normalize_caption`, `publish.upload_post.build_payload` | `test_caption_starts_with_ad_all_platforms`, `test_ad_then_copy_then_required_tags` |
| 3 | Every rendered clip has the "Ad" label from 0.0 s to ≥ 3.0 s, recorded in the manifest and checked by QA. Since layout 2 (2026-11-25) the label sits at the left edge under the hook's largest possible box, outside the platform UI zones (top 150 px, right 15 %, bottom 20 %), on a semi-opaque (75 %) dark box so it reads on bright or busy footage; the manifest records its box, `ad_label_layout` and `ad_label_box`, and QA fails a layout-2 clip whose label is under platform UI or has no box. Clips rendered before layout 2 keep the timing checks only. A campaign overlay never covers it: the label moves down the left edge to a spot clear of the overlay's visible pixels (still clear of the hook, captions and platform UI), or the render is refused; the overlay is composited under the label, hook and captions, never covers the captions, and a full-duration overlay spans the whole clip. | `render.overlays`, `render.campaign_overlay`, `compliance.assert_render_manifest` (`overlay_problems`) | `test_ad_label_first_three_seconds`, `test_ad_label_never_covered_by_campaign_overlay`, `test_full_duration_overlay_covers_the_whole_clip`, `test_overlay_never_relaxes_the_ad_label_timing`, `test_render_keeps_ad_label_clear_of_any_overlay_placement`, `test_real_render_overlay_whole_clip_and_ad_label_visible`, `test_layout2_label_must_be_out_of_platform_ui_and_boxed`, `test_layout2_keeps_the_three_second_rule`, `test_older_manifests_keep_their_rules`, `test_new_renders_use_layout2`, `test_layout2_label_never_covered_by_campaign_overlay` |
| 4 | Paid-content flags are always set: TikTok `brand_content_toggle=true`; YouTube `hasPaidProductPlacement=true`; AI/synthetic flags false. No code path generates synthetic faces or voices. **One exception, TikTok in `PUBLISH_MODE=private` only:** the post is sent with `privacy_level=SELF_ONLY`, `brand_content_toggle=false`, `brand_organic_toggle=false` and no `paid_partnership`, `disclose_commercial`, `branded_content` or `your_brand` field in any spelling (see note 4 below). Outside private mode TikTok is always `privacy_level=PUBLIC_TO_EVERYONE` and `brand_content_toggle=true`. A payload that breaks either rule raises `ComplianceError` before anything is sent. | `publish.upload_post.build_payload`, `compliance.assert_tiktok_disclosure` (also run on the final form in `UploadPostClient.publish`) | `test_disclosure_flags_per_platform`, `test_tiktok_private_is_self_only_and_never_branded`, `test_tiktok_caption_starts_with_ad_in_every_mode`, `test_guard_raises_on_bad_private_payload`, `test_guard_raises_when_private_toggle_is_missing`, `test_guard_raises_on_unbranded_or_private_live_payload`, `test_youtube_private_keeps_paid_product_placement`, `test_bad_private_payload_is_never_sent`, `test_private_request_body_has_no_branded_field` |
| 5 | No post is published without an `approvals` row with decision `approve` by the operator, made after the clip's latest render. | `compliance.assert_human_approved`, `publish.dispatcher` | `test_publish_requires_fresh_human_approval` |
| 6 | No publishing on an evening that is not armed. | `publish.dispatcher` | `test_unarmed_evening_publishes_nothing` |
| 7 | A post maps to exactly one campaign submission; a rejected post is never resubmitted; no clip file is published twice to the same platform. | DB unique constraints + `compliance.assert_unique_post` | `test_no_duplicate_platform_post`, `test_one_submission_per_post` |
| 8 | No engagement automation: the codebase never calls like/follow/comment/DM endpoints and never sends `first_comment`. | `publish.upload_post.build_payload`, static test | `test_no_engagement_fields_or_endpoints` (greps `src/` for forbidden names) |
| 9 | X captions contain no URLs; political campaigns never publish to TikTok or X. A campaign whose required caption text is a link cannot list X. | `compliance.normalize_caption`, `compliance.allowed_platforms`, `ingest.registry.validate_campaign_setup` | `test_x_caption_has_no_urls`, `test_political_blocked_tiktok_x`, `test_url_required_tag_never_goes_on_x`, `test_url_caption_text_refused_with_x` |
| 10 | Clips are 10–90 s and contain no added music track. The only extra input allowed is one campaign overlay: a plain local `.png` still, opened with `-an -sn -dn`; filtergraph input pads may only be `[0:v]`/`[1:v]`; audio is mapped only from the source. | `qa.checks`, `render.encode` (only source audio is mapped) | `test_duration_bounds`, `test_only_source_audio_mapped`, `test_campaign_overlay_input_adds_no_audio`, `test_overlay_input_pads_and_flags` |
| 11 | Publish slots fall inside the operator's configured window. | `publish.slots`, `config` validation | `test_slots_inside_operator_window` |
| 12 | API spend stops at the monthly cap. | `costs.guard` | `test_spend_cap_pauses_pipeline` |

**Note 4: the private-mode TikTok exception.** TikTok does not allow branded content on a private post: it "can only
be configured with visibility as public/friends", and with "only me" the Branded Content option must be disabled
(https://developers.tiktok.com/docs/en/content-sharing-guidelines, checked 2026-11-23). Upload-Post refuses
`paid_partnership=true` with `privacy_level=SELF_ONLY` (HTTP 400 `commercial_disclosure_privacy_conflict`). It
publishes `disclose_commercial` + `branded_content=true` with `SELF_ONLY` as `PUBLIC_TO_EVERYONE`
(https://docs.upload-post.com/api/upload-video and https://docs.upload-post.com/guides/paid-partnership, checked
2026-11-23). So a private test post either fails or goes out publicly, and a public post is the outcome this mode
exists to prevent. A `SELF_ONLY` post is visible only to the operator, so nobody else sees it without the label. The
exception covers only the branded flags: the caption still starts with `#ad` (invariant 2), the render still has the
"Ad" label (invariant 3), and the AI flags stay false. YouTube private posts keep `hasPaidProductPlacement=true`. A
private-mode TikTok post must never be switched to public by hand. Delete it after the test, and republish in live
mode if it should go public.

`test_real_render_overlay_whole_clip_and_ad_label_visible` (invariant 3) needs ffmpeg: it skips on the Mac and only
counts when it runs in CI or inside the clipforge image (Debian ffmpeg 5.1, what production renders with).

Also enforced (no separate number): tests never run with `PUBLISH_MODE=live`
(`tests/conftest.py` fails the session if it is set), and the bot ignores non-owner chats
(`test_bot_ignores_other_chats`).
