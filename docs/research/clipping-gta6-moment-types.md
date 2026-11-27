# GTA 6 clips: which moments get views, and what the app should detect (24 Nov 2026)

Scope: streamer-clipping campaigns (Boeshi, Dylan Page, AsianJeff etc.), where footage is a streamer's GTA 6 VOD. Evidence is launch-week only (19-24 Nov 2026), so treat it as a starting prior, not settled fact.

> moments-v2 (26 Nov 2026): the starting values below (trigger+reaction gate, 2-5 s pre-roll, four scores with
> weights 0.35/0.25/0.25/0.15) were replaced by content-first finding, a six-part rubric (hook, arc, arousal,
> sendability, clarity, density; 0.2/0.2/0.2/0.2/0.1/0.1, `gta6-streamer-v2`) and a keyframe check. See
> `config/moment_profiles/gta6-streamer.yaml` and docs/ARCHITECTURE.md §4.3. This page is kept as the evidence base.

## Bottom line
Views follow a **visible human reaction to something going wrong, absurd or escalating**, not gameplay quality. Walking, driving and exploring with no event is the "boring" footage. A clip needs a trigger the viewer understands in 1-2 seconds, a reaction (face or voice), and a cut right after the payoff.

## Moment types, ranked by strength of evidence

| # | Moment type | Evidence | Detectable by |
|---|---|---|---|
| 1 | **Streamer blunder with a big reaction** (misclick, wrong button, deleting or powering off, self-inflicted loss) | Streams Charts top GTA 6 Twitch/Kick clips: #1 (519K) was a misclicked private-dance purchase, about 6x the #2 clip. Also in the top 10: powering off a PS5 and losing 11+ hours (69K), deleting the game file instead of closing a menu (59K). Of the 6 top-10 clips whose moment is described, 3 are blunders | audio spike + exclamation in transcript ("no", "what", "bro"), then a pause or laugh; menu/UI on screen just before |
| 2 | **Shock or disbelief at something new** (first look, speechless pause, "is that a streamer?") | Eklipse: Kai Cenat's one-line reaction turned an NPC into a clip chain. Kai jumping at a six-star wanted level is a top-10 clip (44K) | face-cam expression change (MediaPipe), loudness jump, silence beat |
| 3 | **Unscripted NPC weirdness** | Infegy: the top social post of launch week was a TikTok of an NPC refusing to drop her selfie stick. Community clip wall: NPC-hunting TikTok 2.79M | NPC/odd behavior is hard to detect visually; rely on streamer reaction + transcript ("look at this guy") |
| 4 | **Glitch or physics fail with rage or disbelief** | Viral X clip of a glitch fist fight where the player is pulled into a cubicle wall; flying cars and vanishing characters widely shared. Crash-then-respawn TikTok 1.96M (0:29); 8s crash TikTok 1.5M | motion/frame-diff spike, ragdoll, then reaction |
| 5 | **Escalation and chaos** (wanted-level jump, police chase turn, crash, flight, boat death) | Vehicles/flying about 9 of the 60 clips on the community wall; flight-reaction TikTok 2.09M | HUD wanted-star change, "WASTED/BUSTED" screens, explosion flash, motion spike |
| 6 | **Chat interplay** (chat warns, streamer ignores; bad advice; message at wrong moment) | Only author-taxonomy support (Oakgen), not measured | chat velocity, if chat is available; transcript mentions of "chat" |
| 7 | **Satire and shock content** (radio ads, animal/activist gags) | One clip passed 8M views on X, but it is not streamer footage and carries policy risk | do not target (see skip list) |

Community clip wall (60 most-watched, 30 of them TikTok), approximate tally: humor/memes/fails about 20, Lucia/Jason character moments about 12, secrets/easter eggs/explainers about 12 (mostly long YouTube), vehicles about 9, police/NPC/wanted about 4. Top short TikToks run 8-35s with 1-3.8M views, but the single biggest (6.37M) is 3:35, so length is not a hard rule. Most of these are creators' own gameplay, not streamer VOD clips.

## Clip structure (consistent across sources)
- Setup the viewer gets in 1-2s, then escalation, payoff, reaction, **end right after the payoff**.
- A missing setup is the most common reason a funny moment dies as a clip. The biggest on-screen moment is rarely the clip itself; the reaction is.
- Starting values (vendor-sourced, calibrate on our own data): start 2-5s before the trigger, 12-35s total, hard cap 45s, hook visible in the first 2s.

## Skip list (hard gates)
- No trigger, or no reaction: walking, driving, menus, loading, inventory, talk about settings or the game with no event. This is probably what produced the boring test clips.
- **Rockstar policy** (page last updated 10 Jan 2025): the ending, big story reveals, edited cutscene compilations, isolated cutscenes, and in-game TV or comedy shown in isolation are removed; leaks and early footage are banned. Cutscenes are allowed inside a larger playthrough. Gate: penalize or reject clips that are mostly cutscene, ending or story reveal, or an in-game show/radio bit played on its own.
- **Monetization flag:** the same policy defines non-commercial use as not making money from the footage. Our earlier note said paid clipping is "not addressed"; the wording suggests paid clipping sits outside the fan-use tolerance. Practical consequence is takedown risk, which is already accepted in the project docs.
- AI-generated fake "gameplay" or leaks (one fake hit 8M views in about 24h before being exposed).
- Sexual or graphic content: the #1 Twitch clip involved a private dance, and the campaign brief lists "banned words" as a rejection reason. Score the blunder and reaction, not the content, and crop or cut to keep it safe. I have not verified TikTok or Instagram policy wording on this.

## App spec (starting point)
1. **Candidate generation** (cheap, over the whole VOD): streamer loudness z-score vs a rolling baseline, pitch jump, laughter, exclamation tokens in the Groq transcript, face-cam expression change, frame-diff motion spike, HUD wanted-star change. Use chat-message velocity only when chat data exists for the supplied footage (campaign files may not include it).
2. **Gates** (reject before ranking): needs a trigger plus a reaction within about 3s of it; reject low audio variance with no visual event; reject spoiler/cutscene-dominant windows.
3. **LLM ranker** (Haiku): classify each candidate into the archetypes above and score 0-10 on reaction intensity, event salience, self-contained setup (a stranger gets it in 3s), and absurdity/relatability. Output archetype, window start/end and a one-line reason.
4. **Initial weights:** reaction 0.35, event 0.25, setup clarity 0.25, absurdity 0.15. These are my guess, not derived from data.
5. **Calibration:** log archetype and scores per posted clip, record 24h and 72h views, and re-fit weights after the first 20-30 clips. This beats trusting any blog's ranking.

## Limits of this research
- Only top-of-distribution numbers are public; I found no median or typical views for streamer GTA 6 clips, so survivorship bias is likely.
- Streams Charts view counts are first-days snapshots from native Twitch/Kick clips; TikTok/Shorts numbers are a different scale.
- No independent data found on Boeshi's content; his campaign brief (humor/meme, relatable captions) is the only guide. Blunder, reaction and absurdity archetypes fit that style.
- Hook-timing and length claims come from clipping-tool vendor blogs (Eklipse, OpusClip, FluxNote), which are marketing sources.
- Peaks on launch day were led by Spanish, Portuguese and Turkish streams; English marathons were long but not huge (Kai Cenat peaked at 126.6K). The Boeshi brief wants 40%+ tier-1 audience, so English-language, tier-1-friendly moments matter. That is my inference, not a sourced finding.

## Sources
- Streams Charts, most-viewed GTA 6 clips: https://streamscharts.com/news/most-viewed-gta-6-clips
- Infegy, week-one social trends: https://www.infegy.com/insight-brief/top-five-social-trends-from-gta-6-week-one
- Community clip wall: https://gta6communitywiki.com/clips
- viralgta6 (YouTube-heavy, ordering unverified): https://viralgta6.com/
- Eklipse, GTA viral clips: https://blog.eklipse.gg/streaming-tips/gta-viral-clips.html
- Eklipse, GTA 5 funny moments (structure, VOD signals): https://blog.eklipse.gg/streaming-tips/gta-5-funny-moments.html
- Oakgen, 25 clip types (author taxonomy, unmeasured): https://oakgen.ai/blog/gta-6-funny-moments-ideas
- Futurism, GTA 6 bugs: https://futurism.com/future-society/gta-6-bugs
- Yahoo/NY Post, un-PC viral moments: https://www.yahoo.com/entertainment/articles/gta-6-fans-wild-game-194927307.html
- Streamer.guide, first 24h streaming: https://streamer.guide/blog/gta-6-first-24-hours-streaming-top-streamers
- Rockstar policy: https://support.rockstargames.com/articles/7bNaeoMFTV0iUDGhStTXvz/policy-on-posting-copyrighted-material
