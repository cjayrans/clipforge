# Claude Code prompts, phase by phase

Run these from the repo root on your Mac. Paste each prompt as written. Each phase ends with a commit or a PR. Start a
session at 8pm, let it work, and review the result the next evening. Before pasting a prompt that says "plan mode",
press Shift+Tab until Claude Code shows plan mode; after you approve the plan, Claude Code switches out of it to
implement.

Before Phase 0: install Claude Code, sign in with your Pro account (`claude` then follow the login), install Docker
Desktop for Intel and `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`), and make sure `gh auth status` works
if you want Claude to open PRs.

---

## Phase 0 — Foundation (one session)

```bash
cd ~/code/clipforge
claude
```

Paste:

```
You are building clipforge. Read CLAUDE.md, docs/PRD.md, docs/ARCHITECTURE.md, docs/COMPLIANCE.md and docs/TASKS.md
in full before doing anything.

Do Phase 0 (tasks P0.1–P0.11) on main. Start in plan mode: show me the plan, the files you will create, and any
place where the docs are ambiguous or conflict. Wait for my OK. Then implement it.

Rules:
- src/clipforge/schemas.py is a provided draft contract. Keep every existing name and field; only add what Phase 0
  needs, and list every addition in your final summary.
- Pre-declare ALL runtime and dev dependencies from TASKS.md P0.1 now, so Phase 1 branches never touch
  pyproject.toml. Commit uv.lock.
- Implement compliance.py for real and write every test in tests/compliance/. Mark tests that need later modules
  xfail(strict=True) with the phase that will make them pass.
- Docker image must be linux/amd64 and build on an Intel Mac. No macOS-only dependencies.
- Run `make lint test` and `docker compose build` before finishing. Commit in small, clear commits.

Finish with: what you built, what you verified, anything you changed in schemas.py, and anything I must do by hand.
```

---

## Phase 1 — Four parallel worktrees

Open four terminal tabs. In each, start Claude Code in its own worktree:

```bash
# Tab A
cd ~/code/clipforge && claude --worktree ingest-transcribe
# Tab B
cd ~/code/clipforge && claude --worktree rank
# Tab C
cd ~/code/clipforge && claude --worktree render
# Tab D
cd ~/code/clipforge && claude --worktree ops
```

If `--worktree` isn't available in your Claude Code version, create the worktrees yourself and run `claude` inside
each:

```bash
git worktree add ../clipforge-A -b feat/ingest-transcribe
git worktree add ../clipforge-B -b feat/rank
git worktree add ../clipforge-C -b feat/render
git worktree add ../clipforge-D -b feat/ops
```

**If you hit the Pro usage limit:** run A + C first, then B + D.

Paste this into each tab, replacing `<LETTER>` and `<BRANCH>` (A feat/ingest-transcribe, B feat/rank,
C feat/render, D feat/ops):

```
You are one of four parallel Claude Code sessions building clipforge. Read CLAUDE.md, docs/PRD.md,
docs/ARCHITECTURE.md, docs/COMPLIANCE.md and docs/TASKS.md in full.

Your job is Phase 1 section <LETTER> in docs/TASKS.md, on branch <BRANCH>.

Ownership rules (other sessions are editing other folders right now):
- Edit only the directories TASKS.md assigns to section <LETTER>, plus their tests.
- Do not edit pyproject.toml, uv.lock, src/clipforge/schemas.py, src/clipforge/db/, src/clipforge/config.py,
  compliance.py or another section's folders. If you need a contract change, write it to
  docs/contract-requests/<BRANCH-without-feat/>.md and stub it locally.
- Code against the models in src/clipforge/schemas.py.

Work plan: plan mode first, show me the plan and wait for my OK. Then implement task by task, with tests for each,
committing after each task. Mock every external HTTP call with respx. For any external API field name or endpoint,
check the official docs URL in ARCHITECTURE.md §6 before writing it, and record what you verified.

Before finishing: run `make lint test`, then use the compliance-reviewer subagent on your diff and fix anything it
fails. Push the branch and open a PR to main (gh pr create) whose description lists: tasks done, tests added,
external facts verified (with URLs), contract requests, and anything left for Phase 2.
```

---

## Phase 2 — Integration (one session, main)

Merge the four PRs on GitHub (A, B, C, then D), pull main, then:

```
Read CLAUDE.md, docs/PRD.md, docs/ARCHITECTURE.md, docs/COMPLIANCE.md and docs/TASKS.md, then read the four merged
PR descriptions (gh pr list --state merged --limit 4, gh pr view <n>) and docs/contract-requests/.

Do Phase 2 (P2.1–P2.6). Plan mode first; wait for my OK. Resolve every contract request in schemas.py and update
the callers. Build pipeline.py, wire New hook / Shorter, write the fully mocked end-to-end test from PRD §7.2, and
remove every xfail in tests/compliance/ so all pass. Finish with `make lint test`, `make up` (both services
healthy), the compliance-reviewer subagent on the whole repo, and a summary of anything still weak.
```

---

## Phase 3 — Live readiness (one session, on the Mac)

First put one real source video in (a file the campaign supplied, or any clip you are allowed to use for testing) `tests/fixtures/local/` (git-ignored) and register the campaign with
`uv run cf campaign add --from-json campaigns/dylan-page-gta6.json`. Set `PUBLISH_MODE=private` in `.env` yourself.

```
Do Phase 3 in docs/TASKS.md. Start with P3.1: run scripts/bench_render.py on tests/fixtures/local/*, then tune
render settings so two one-hour sources finish a nightly run in under 3 hours on this Intel Mac (Docker, 6 CPUs).
Then help me run P3.2: one private-mode run end to end. I will check the private TikTok post (Instagram is skipped in private mode) and tell
you what to fix. Then write docs/OPERATIONS.md from the real implementation (P3.3). Don't change PUBLISH_MODE —
I do that.
```

After P3.2 passes, set `PUBLISH_MODE=live` in `.env` yourself, `make down && make up`, and start the 60-day test.

---

## Useful follow-ups

- **A test is failing and you don't know why:**
  `Run make test. For each failure, find the root cause, fix it, and explain the fix in one line each. Don't weaken
  any test in tests/compliance/.`
- **Resume after a usage limit:**
  `Read docs/TASKS.md and git log --oneline -20. Tell me which tasks in my section are done and continue with the
  next one.`
- **Monthly upkeep:**
  `Check for outdated dependencies with uv, upgrade minor versions, run make lint test, and summarise anything that
  changed in the Anthropic, Groq, Upload-Post or Telegram APIs we call (check the docs URLs in ARCHITECTURE.md §6).`
