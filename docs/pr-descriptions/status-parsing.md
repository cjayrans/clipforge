# Fix: a TikTok post that was still processing was recorded as failed (`fix/post-status-parsing` → `main`)

On 24 Nov the 13:10 private-test slot published clip 7 to TikTok, but clipforge marked the post **failed** and raised
`publish_failed: platform reported failure`. TikTok had in fact published it (`SELF_ONLY`). This PR fixes the status
parsing, handles the "published but no link yet" case that TikTok produces, and adds a safe repair for the posts that
were wrongly marked failed. Written for Carson.

## Root cause

`interpret_status` (`publish/upload_post.py`) treated `success: false` on a platform entry as a failure:

```python
if entry.success is False and entry.status != "retryable":
    return StatusOutcome("failed", error=entry.message or "platform reported failure")
```

The status endpoint reports `success: false` for a job that has not finished. Observed live for post 1, 23 s after it
was accepted: `status: "processing"`, entry `{platform: tiktok, status: "processing", attempts: 1, success: false}`
(no message, hence our fallback text `platform reported failure`). Minutes later the same job read `status: "completed"`,
`success: true`, `error_code: null`, `post_url: null`, and a warning that TikTok "has not released its public post ID yet
(it does so once moderation finishes, which can take hours)". Failed posts are never polled again, so the state stuck.

## What changed

### 1. `interpret_status` branches on the status, not on `success`
| Upload-Post entry | Result |
| --- | --- |
| `queued`, `pending`, `processing`, `in_progress` (any `success`) | pending, keep polling |
| `retryable` | pending, keep polling |
| `failed`, `skipped`, `fallback_to_inbox` | failed, with `error_code: error_message` when present |
| finished (`completed`, or no entry status and a finished top-level status) with `success: false` | failed |
| finished with `success: true` and a `post_url` | live |
| finished with `success: true` and no `post_url` | **published, link not released yet** (pending, not a failure) |

Also parsed now: `upload_timestamp` as the `{"$date": "..."}` object the endpoint returns (it was silently dropped, so
`published_at` fell back to "now"), `error_message` / `error_code` and `warnings`. The docs' in-progress example
(`success: true, message: "Queued"`) is not mistaken for "published". A job that Upload-Post itself reports as
queued/processing is given 1 h before we call it stuck (was 10 min); an entry-less pending status keeps 10 min.

### 2. Completed but no `post_url` (what the old `published_without_url` path did, and the fix)
**Before:** the post stayed `PUBLISHING` until 10 min after it was created, then was marked FAILED ("may be live - find it
by hand"). No submission row existed (it was only created together with a URL), so **no SUBMIT NOW notice was ever
sent** and the 30-minute Content Rewards window passed silently, while TikTok can hold the link for hours. That was wrong.

**Now:** the post stays `PUBLISHING` (still polled), but is marked published: `published_at` = Upload-Post's upload time,
an events row says so, and the submission is opened at once with `deadline_at = published_at + 30 min`. The worker
then sends the SUBMIT NOW notice without a link:

```
SUBMIT NOW: LINK NOT AVAILABLE YET
PRIVATE TEST POST (only you can see it): do NOT submit it to the campaign.      <- private mode only
<campaign> - tiktok
TikTok published this post at 13:12 but has not released its link yet (it can take hours).
1. Open the TikTok app, your profile, the newest video (caption starts "#ad A question about ...").
2. Share, then Copy link.
3. Send it to this bot: /posturl 7 <the link you copied>
Submit here: <campaign submission link>
Deadline: 13:42, which is 30 min after it was published at 13:12. The 30 minutes count from the publish time, not from when you get the link.
```
(buttons Submitted / Missed as before; the +20 min reminder also fires and repeats the missing link and the command).
The normal notice (with a URL) now also carries the private-test warning in private mode. Warning text is chosen from
the current `PUBLISH_MODE`.

`/posturl <post id> <link>` (bot, owner only; `publish/manual_url.py`): stores a link you typed. It must be `https`, on
the post's own platform domain (tiktok.com for TikTok; no look-alike hosts, no credentials, not the bare site), and the
post must be published-without-link. Moves PUBLISHING → LIVE with `actor="operator"` and an events row; the deadline
stays at publish time + 30 min. Nothing is fetched from any platform.

### 3. Re-poll for completed posts without a URL
`poll_pending_posts` keeps polling them: every 30 s tick for the first 30 min, every 5 min until 24 h, hourly after. When
the URL arrives the post goes LIVE (publish time unchanged, one submission) and a `post_url_ready` alert tells you the
link. **Only if no URL has come 24 h after publishing** does it raise one `publish_unknown` alert (once), which says to
find the link and use `/posturl`. The post is never failed for a missing link.

### 4. Repair for posts 1, 2 and 3 (not run)
`cf post repair <post id>...` (dry run unless `--apply`; `publish/repair.py`). Per post it:
1. refuses unless the post is FAILED and has an `upload_post_job` (a post that never reached Upload-Post would need a
   re-send, which is refused);
2. does one **GET** of the Upload-Post status and refuses unless Upload-Post says the upload completed (a really failed
   or still-running job is left alone);
3. with `--apply`: `db.transition(post, PUBLISHING, actor="operator")` with an events row that keeps the old error text,
   then the poller's own code puts it LIVE (if there is a URL) or "published, link pending". No raw SQL.

It never calls `UploadPostClient.publish`, creates no post and does not touch the clip, so invariant 7 holds (same row,
same Upload-Post job; the resend path only fires when `upload_post_job` is empty). To run after this is deployed:

```bash
export PATH=/Applications/Docker.app/Contents/Resources/bin:$PATH
docker compose exec worker cf post repair 1 2 3            # dry run: shows what Upload-Post says for each
docker compose exec worker cf post repair 1 2 3 --apply    # repairs
```
Expect: each repaired post opens a submission whose deadline (publish time + 30 min) has already passed, so the worker
sends one "LINK NOT AVAILABLE YET" notice per post, with the private-test warning. Tap **Missed** on them (they are
private test posts), or send `/posturl <id> <link>` if you want a real URL recorded.

## Contract and test changes (please read)

- **`schemas.py` (the contract):** `POST_TRANSITIONS[FAILED]` was empty and is now `{PUBLISHING}`. This is needed so the
  repair can use `db.transition`. It is the only edit. Nothing but `publish/repair.py` moves a post out of FAILED.
  `SubmissionNotice` is untouched (the link-less notice uses a local dataclass).
- **No test in `tests/compliance/` changed.**
- **One existing test replaced on purpose:** `tests/publish/test_dispatcher.py::test_success_without_url_says_the_post_may_be_live`
  asserted the old behaviour (failed after 10 min with "may be live"). It is replaced by
  `test_completed_without_url_is_published_not_failed` and the tests around it. `test_poll_failure_and_timeout` and every
  other existing assertion are unchanged. `tests/test_cli.py::test_help_lists_every_command` now also lists `post`.
- New tests: processing with `success: false` (several in-progress states x success values), completed with
  `success: true` and null `post_url` (using the shape captured live, personal fields replaced), failed with an error
  code, retryable, give-up timers, throttle, 24 h alert once, late URL keeps the deadline, a published post is never
  re-sent (the `/upload` route is never called), repair dry run / apply / refusals / no HTTP POST / operator actor and
  events rows, the link-less notice and reminder text, `/posturl` (valid, invalid, wrong state, owner only).
- New alert kind `post_url_ready` (`review/alerts.py`); docs: `ARCHITECTURE.md` (post status interpretation), `PRD.md` §4.3.

## Not verified

- The shape of a **failed** entry's `error_code` / `error_message` values has not been seen live (only the keys, which
  are `null` on success). The tests use the docs' code names.
- `GIVE_UP` for an in-progress job (1 h) follows Upload-Post's own "no activity for over 1 hour" rule from its docs.
- I did not touch the running containers: the worker and bot still run the old image until you rebuild.

## Verification

- `make lint test`: ruff clean; 1193 passed, 11 skipped, 1 deselected (the 11 skips need ffmpeg and run in CI).
- `mypy --strict` on `schemas.py` and `compliance.py`: clean.
- compliance-reviewer on the staged diff, first pass: **FAIL** on one item, invariants 1-12 and the other rules held
  (219 compliance tests pass; `tests/compliance/` has no changes). The FAIL: one of my new tests set the publish mode
  to the live value through the environment (it only formatted message text, nothing was published), and no test may.
  Fixed: `test_non_private_notice_has_no_private_warning` uses the default dry_run settings; no test sets live.
  Its hardening suggestion is applied: `tests/publish/test_post_transitions.py` asserts that only `dispatcher.py` and
  `repair.py` move a post into PUBLISHING, that FAILED may only go to PUBLISHING, and that `repair.py` never calls
  `.publish(`. It also noted a narrow race between `/posturl` and the poller; `/posturl` now turns an
  `IllegalTransition` into a "changed while saving, try again" reply (tested).
  Second pass on the fixed diff: **PASS** for invariants 1-12 and the other rules; no test sets live mode.
- CI: see the PR checks.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
