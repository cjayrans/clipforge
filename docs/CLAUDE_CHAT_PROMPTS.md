# Prompts for Claude chat (non-coding work)

Use these in the claude.ai "Clipping" project. They cover the setup and weekly work that doesn't need Claude Code.
Paste the text between the fences and fill in the brackets.

---

## 1. Campaign brief → registry entry (Sunday, every new campaign)

Copy the campaign page text from Content Rewards (or Vyro) yourself and paste it in. Don't ask Claude to log in or
browse the marketplace for you: their terms don't grant permission to scrape.

```
Here is a clipping campaign brief I copied from [Content Rewards / Vyro]:

<<<
[paste the full campaign page text, including rate, budget, caps, minimums, platforms, rules, and footage links]
>>>

1. Check it against my filter and give a pass/fail per line:
   - content type is clipping and the footage is supplied by the campaign or is a named streamer's stream the rules allow clipping from, not my own gameplay; say exactly where the footage comes from and whether downloading it is allowed
   - TikTok and Instagram are both allowed (YouTube and X are a bonus)
   - approval rate is 40% or higher
   - budget remaining is at least $1,000 and at least 5× the per-clip cap, with fewer than 150 participants
   - say which viewer countries are unpaid, and whether views with no country data are paid
   - the minimum payout is reachable at 10,000 views or fewer (show the maths)
   - no "Submissions on hold"; the brief doesn't ban AI-assisted editing
   - not gambling, crypto, finance, supplements, political, or kids content
2. If it passes, output ONE JSON object that validates against this schema (clipforge CampaignIn), with nothing
   invented — use null or [] where the brief is silent and tell me which fields those are:
   marketplace ("content_rewards"|"vyro"), name, url, submission_url, cpm_usd, min_payout_usd, min_views_per_post,
   per_clip_cap_usd, budget_remaining_usd, approval_rate_pct, allowed_platforms (["tiktok","instagram","youtube","x"]
   subset), required_tags, disclosure_text, rules_notes (every rule in the brief, verbatim where short), is_political,
   priority (0), source_urls (only footage links the brief provides), content_kind ("talk"|"game"),
   footage_mode ("supplied"|"streamer_stream"|"own_gameplay"|"official_broadcast"), allow_stream_download (true only if the rules explicitly allow downloading the streamer's Twitch VODs/clips), cpm_by_platform and min_payout_by_platform
   (objects keyed by platform; only where the rates differ), unpaid_viewer_countries, participants, budget_total_usd.
3. List anything in the brief that my pipeline would need to handle differently (required on-screen text, banned
   words, length limits, watermark rules).
```

Save the JSON in `campaigns/` (for the GTA 6 campaigns, update the matching file, e.g. `campaigns/dylan-page-gta6.json`), then run
`uv run cf campaign add --from-json campaigns/<file>.json`.

---

## 2. Weekly review (Saturday, after reconcile)

Run `make weekly-report` (it writes `data/reports/weekly-YYYY-MM-DD.md`) and paste it in.

```
Here is my clipforge weekly report:

<<<
[paste report]
>>>

Act as a blunt performance analyst. Using only the numbers in the report:
1. Which campaigns should I drop, keep, or push harder, and why (one line each)?
2. What do the top 5 clips' hooks and topics have in common versus the bottom 5?
3. Propose at most 3 concrete edits to prompts/score_windows.md or prompts/clip_copy.md, written as a diff, each
   tied to evidence in the report.
4. Where am I against the kill/scale rules (day 60: 100k paid views/month; day 120: stop; scale at 15%+ posts
   clearing minimums for 4 weeks)?
Keep it under 250 words plus the diff.
```

---

## 3. Monthly policy and price check (first Sunday)

Needs web search on.

```
Today is [date]. Check for changes since [last check date] to each of these, using the official pages, and give me
a table: item, what changed (or "no change found"), date, source link, and whether my setup needs to change.
- Content Rewards Creator Terms (https://contentrewards.com/terms): submission window, fees, account rules,
  disclosure, category bans
- Vyro help center: minimum views per post, fees, payout method
- TikTok Community Guidelines on unoriginal content and multiple accounts; TikTok Content Posting API rules
- YouTube reused-content and Shorts recommendation policy
- Instagram originality / recommendation rules
- Upload-Post pricing and API changes
- Anthropic and Groq pricing for claude-haiku-5-5, claude-sonnet-5-5 and whisper-large-v3-turbo
Flag anything that touches the 12 rules in my COMPLIANCE.md.
```

---

## 4. Caption style presets (one-off, before Phase 1 C)

```
I run one clipping account set in the gaming niche on TikTok and Instagram Reels (YouTube Shorts later). Design 3
burned-in caption style presets that read well on a phone, stay clear of the bottom 20% of the screen (platform UI),
and look distinct from each other. For each give: name, font (must be free under the SIL Open Font License, with
its Google Fonts link), size at 1080×1920, primary colour, highlight colour for the active word, outline or box,
vertical position as % of height, words per line. Output as YAML matching this shape:
  <name>: { font: "...", size: 78, primary: "#FFFFFF", highlight: "#FFD400", outline: 6, y_pct: 66 }
```

Paste the YAML into `config/clipforge.yaml` under `styles:` and download the fonts into `assets/fonts/`.
