# Campaigns: niche decision and screening rules

Facts below were read from Content Rewards campaign cards on 22 November 2026 (GTA 6 launched 19 Nov 2026). Numbers change
daily and budgets can drain in days; recheck before joining. The brief, rules and reference files are hidden until you
join, so none of the rules below have been read yet.

## Decision

- Niche: Grand Theft Auto 6. Rockstar supplies no footage. Content Rewards GTA 6 campaigns mostly clip named streamers,
  plus sound and logo campaigns.
- Footage comes from (1) files or links the campaign owner supplies after you join, (2) the streamer's public Twitch
  streams where the rules allow clipping and downloading, (3) files Carson downloads himself and drops in `inbox/`.
  YouTube is never downloaded (YouTube's terms), even when a campaign allows clipping.
- Take-Two can take GTA content down at any time. Footage supplied by a campaign is licensed for that campaign only.
- Avoid: pool campaigns that need roughly 400K–1M views to qualify, logo campaigns ($0.30–$0.75), campaigns needing a
  dedicated account of their own, gambling-sponsored campaigns.

## Evidence (read 22 Nov 2026)

| Campaign | Rate per 1,000 views | Min / max payout | Budget (spent / total) | Clippers | Footage | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| Dylan Page GTA VI Marathon (NewsDaddy) | $1.20 on TikTok, Instagram, YouTube, X | $4 (≈3,300 views) / $250 | $79 / $5,000 | 4 | His 120-hour YouTube marathon; clips must show Dylan, caption includes #DylanPage | First |
| AsianJeff (Reach) | $1.00 on all four | $10 (10,000 views) / $300 | $722 / $2,500 | 12 | His GTA 6 Twitch playthrough | Second |
| Lucky Latvian x GTA6 (KLIPZY) | $2.00 on TikTok, Instagram, X, YouTube | $10 (5,000 views) / $100 | $426 / $1,000 in 2 days | 63 | Stream moments, CTA required, EU focus | Budget likely gone |
| Dasgasd3m x RUCKUS | $2.00, TikTok and YouTube Shorts only | $20 (10,000 views) / $150 | $94 / $1,000 | 49 | GTA 6-specific accounts required | Skip (no Instagram, needs dedicated accounts) |
| Boeshi GTA 6 Clipping (ClipLaunch), added 24 Nov | $1.00 on TikTok, Instagram | not read / $300 | $132 / $1,750 (8% paid, 24 Nov) | not read | Boeshi's recent Twitch VODs (twitch.tv/darealboeshi); overlay on the whole clip; caption includes twitch.tv/darealboeshi | Active (`campaigns/boeshi-gta6.json`) |
| DiegoLoveless (ClipLaunch) | $1.00 on TikTok, Instagram, YouTube | $2 / not read | $926 / $1,400 | 126 | Must feature Diego plus a CTA to his Twitch | Crowded |

Dylan Page's footage is a YouTube marathon: under the rules above, only use it if the campaign supplies files or links
you may download, or the rules explicitly permit it, otherwise skip the campaign.

## Screening rules (apply to every new campaign)

1. Content type is clipping, and the footage is supplied by the campaign or is a named streamer's stream that the
   rules say may be clipped. Never your own gameplay.
2. The rules say where the footage comes from and whether downloading it is allowed. If unclear, ask the campaign owner
   or skip.
3. It accepts TikTok and Instagram.
4. Budget left is at least $1,000 and at least 5× the per-clip cap, with fewer than 150 clippers.
5. The minimum payout is reachable at 10,000 views or fewer.
6. Your viewers are paid (check unpaid countries and the no-country-data rule).
7. The launch or event is still fresh.
8. Approval rate is 40% or higher where shown; no "Submissions on hold"; AI-assisted editing is allowed.
9. Not gambling-sponsored, political or regulated.

`cf campaign check <id>` applies the thresholds in `config/clipforge.yaml` (`campaign_filter`).

## Adding a campaign (no code changes)

Everything campaign-specific is data. A new campaign is one JSON file plus its assets:

1. Write `campaigns/<slug>.json` against `schemas.CampaignIn` (copy an existing file). Generic optional fields:
   - `required_caption_tags` (or `required_tags`): text every caption ends with, after `#ad` and the copy, e.g.
     `["#Tag"]` or a channel link like `["twitch.tv/<channel>"]`. A link cannot be combined with `x` in
     `allowed_platforms` (X captions carry no URLs); `cf campaign add` refuses it.
   - `copy_style_notes`: the tone the brief asks for (e.g. humor/meme); fed to the hook/caption prompt.
   - `moment_profile`: name of a file in `config/moment_profiles/` (e.g. `gta6-streamer`) for streamer/game footage.
     The moment ranker then picks reaction moments by archetype instead of transcript windows. `cf campaign add`
     refuses an unknown or invalid profile. Leave it out for talk content.
   - `overlay`: `{"path": "campaigns/assets/<slug>/<file>.png", "position": {"x_pct": 50, "y_pct": 31.5},
     "scale": 0.6, "full_duration": true}`. The PNG needs an alpha channel. `cf campaign add` prints where its
     visible part lands and refuses a placement that would cover the captions, the hook, the platform UI (top
     150 px, right 15 %, bottom 20 %) or leave no spot for the "Ad" label. Fix the numbers in the JSON; the code never
     moves or shrinks it.
   - `layout` (game footage): `auto` (default), `stacked`, `crop` or `fit`. `auto` puts the streamer's facecam on
     top (1080x640) over a gameplay crop (1080x1280) when a facecam is found and a face is in it for the clip, else a
     full-height 9:16 gameplay crop, else the whole frame over a blurred copy; each clip records the layout used and
     why better ones were skipped. A set layout is used or the render is refused. `stacked` needs `content_kind:
     game`. Talk footage keeps the face crop.
   - `facecam_box`: `{"x_pct": .., "y_pct": .., "w_pct": .., "h_pct": ..}` (top-left corner and size in percent of
     the source frame). Only when facecam detection gets it wrong; check what detection found with
     `cf source facecam <source id> --debug-image <file>.jpg` (`--redetect --no-store` to look without saving). The debug images are frames of the campaign's licensed
     footage: keep them on this machine, use them only to check the box, and delete them afterwards.
   - `allow_stream_download` (only when the rules say so), `stream_channels` (Twitch logins the footage may come
     from; a VOD is matched on its channel login, a clip on the channel's display name lowercased, so a clip from a
     channel whose display name differs from its login is refused: the safe direction), `max_source_minutes` (download only the first N minutes), `source_urls`.
   - Unknown numbers: `null` for `min_payout_usd`/`min_views_per_post` (stored as 0) and the optional ones; say what was
     not read in `rules_notes`.
2. Put its assets in `campaigns/assets/<slug>/` (git-ignored: licensed to the operator for that campaign only; the
   compose file mounts `campaigns/` read-only into the containers).
3. `uv run cf campaign add --from-json campaigns/<slug>.json`, then `cf campaign check <id>`.

Rules a brief states that the code cannot enforce (account audience, engagement rate, how long a post stays live,
limits on near-identical clips) go in `rules_notes` and are the operator's to keep.

## Sources

- https://contentrewards.com/discover (GTA 6 filter, 22 Nov 2026)
- https://contentrewards.com/articles/highest-paying-clipping-niches
