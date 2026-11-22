# clipforge — Claude Code handoff package

This package is everything Claude Code needs to build **clipforge**: a ban-safe, semi-automated pipeline that turns
campaign-supplied or campaign-permitted long-form footage into vertical clips, gets your approval on Telegram, publishes through
Upload-Post, and tracks Content Rewards submissions and payouts.

Business context, the weekly operating rhythm and the setup runbook live in the doc
**Clipping Business Plan & Operator Runbook**. This package covers only the build.

## What's in here

| Path | Purpose | Who reads it |
| --- | --- | --- |
| `CLAUDE.md` | Project memory: stack, commands, rules, layout | Claude Code, every session |
| `docs/PRD.md` | What to build and how we know it works | Claude Code, you |
| `docs/ARCHITECTURE.md` | Technical design: components, data model, APIs, scheduling | Claude Code |
| `docs/COMPLIANCE.md` | The 12 invariants and the test that enforces each | Claude Code, you |
| `docs/TASKS.md` | Phased task list, split for parallel worktrees | Claude Code |
| `docs/CAMPAIGNS.md` | Niche decision, campaign screening rules, and the evidence behind them | You, Claude Code |
| `campaigns/` | Campaign registry entries (JSON, validated by `schemas.CampaignIn`); starts with the two GTA 6 streamer campaigns | You, Claude Code |
| `docs/PROMPTS.md` | The exact prompts to paste into Claude Code, phase by phase | You |
| `docs/CLAUDE_CHAT_PROMPTS.md` | Prompts for Claude chat (non-coding setup and weekly reviews) | You |
| `src/clipforge/schemas.py` | The shared interface contract every module codes against | Claude Code |
| `config/clipforge.example.yaml` | Slots, styles, caps, campaign filter thresholds | You, Claude Code |
| `.env.example` | Every secret the app reads | You |
| `.claude/settings.json`, `.claude/hooks/guard.py` | Permission rules and a hook that blocks secret reads and destructive commands | Claude Code |
| `.claude/agents/compliance-reviewer.md` | A subagent that reviews diffs against COMPLIANCE.md | Claude Code |

## How to use it

1. Create a private GitHub repo named `clipforge`, clone it to your Mac, and copy this whole folder's contents into
   the repo root (including the hidden `.claude/` folder and `.env.example`).
2. `git add -A && git commit -m "Handoff package" && git push`.
3. Copy `.env.example` to `.env` and fill it in (`.env` is git-ignored and Claude Code is blocked from reading it).
4. Join your first GTA 6 campaign (Dylan Page or AsianJeff) only after Phase 2 passes, read its hidden brief, and fill the
   blanks in `campaigns/dylan-page-gta6.json` (the Setup walkthrough, Part 5, has the steps).
5. Open `docs/PROMPTS.md` and run Phase 0. Then Phases 1–3 in order.

Expect about 2–3 weeks of evening sessions on Claude Pro. If you hit the usage limit, run two Phase 1 worktrees at a
time instead of four (PROMPTS.md says how).
