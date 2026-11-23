# Contract requests: `feat/ingest-transcribe` (Phase 1 A)

None of these block merging A. Each is stubbed locally in `ingest/` or `transcribe/`. Phase 2 (P2.1) resolves them.

## CR-A1. Operator alert delivery (bot)

**Need.** ARCHITECTURE §4.1 and PRD F17 ask for an operator alert in several cases: a refused host, no speech, a
download failure, a transcription fallback or failure, and an inbox folder with an unknown or inactive campaign.
Section A owns no Telegram code.

**Stub.** `clipforge.ingest.alerts.alert_operator(session, kind, entity_id, message)`:
- Writes an `events` row with `entity="alert"`, `to_state=<kind>`, `from_state=None`, `actor="system"`,
  `detail=<message>`.
- Logs a WARNING.

Kinds are listed as constants in `ingest/alerts.py`: `host_refused`, `campaign_inactive`, `no_speech`,
`download_failed`, `duplicate_source`, `inbox_unknown_campaign`, `transcribe_failed`, `transcribe_fallback`.

**Proposal.** The bot (D) needs to know which alerts it has already sent. Pick one:
- (a) A `notified_at` column on `events`.
- (b) A `FlagRow` key `alerts_sent_through` holding the last sent `events.id`.

Option (b) needs no schema change. Then a bot job, `review.bot.send_pending_alerts`, runs every 60 s and sends each
new alert row.

## CR-A2. `transcribe` config block (local fallback flag)

**Need.** TASKS A5 puts the faster-whisper fallback "behind a flag". `AppConfig` is `extra="forbid"`, and Phase 1 may
not edit `config.py`.

**Stub.** `transcribe.local.local_fallback_enabled(settings)` and `local_model_name(settings)` read
`settings.app.transcribe.local_fallback` and `.local_model` when they exist. Until then they default to `True` and
`"large-v3-turbo"`.

**Proposal.** Add to `AppConfig` and to `clipforge.example.yaml`:

```yaml
transcribe:
  local_fallback: true        # faster-whisper on CPU when Groq fails twice (slow)
  local_model: large-v3-turbo # faster-whisper size name; "tiny" for tests
  language: en                # passed to Groq and faster-whisper
```

The `language` key would replace the `"en"` default in `GroqTranscriber`/`LocalTranscriber`. It is optional.

## CR-A3. CLI: output and exit codes for registry commands

**Need.** `cli._delegate` discards return values, and `cf campaign check` cannot set a non-zero exit code when a check
fails.

**Stub.** The registry functions print their own summaries. Operator errors raise `ingest.registry.RegistryError`,
which subclasses the ClickException that typer handles, so `cf` prints `Error: ...` and exits 1.
`check_campaign` returns `list[QACheck]` and prints PASS/FAIL, but exits 0 either way.

**Proposal (optional).** Exit 1 from `cf campaign check` when any returned check failed.

## CR-A4. Signatures in `ingest/__init__.py` docstring

The docstring lists `list_campaigns(session)` and `set_campaign_status(session, campaign_id, status)`. `cli._delegate`
calls every target as `fn(session, settings, *args)`, so the implemented signatures are:
- `list_campaigns(session, settings=None)`
- `set_campaign_status(session, settings, campaign_id, status)`

**Proposal.** Update the docstring in P2.1.

## CR-A5. Pipeline wiring (P2.2)

These are not contract changes, but Phase 2 needs to know them:

- **Inbox scan.** Call `ingest.inbox.scan_inbox(session, settings)` at the start of the nightly run, before
  downloading.
- **Download errors.** `ingest.download.download_source` raises `ComplianceError` or `DownloadError` *after* flushing
  the FAILED status and the alert. Commit the session after catching either one. A `DownloadError` leaves the source
  REGISTERED, so the next night retries it.
- **Transcription.**
  - `transcribe.service.transcribe_source` returns a `Transcript`, and rank only sources whose status is TRANSCRIBED.
  - A no-speech source returns a stored transcript but is marked FAILED `no_speech`.
  - `costs.SpendCapReached` propagates unchanged.
  - Other failures raise `TranscriberError` after an alert, and the source stays DOWNLOADED.
- **Purge.** Schedule `ingest.purge.purge_sources(session, settings)` at 03:30 (ARCHITECTURE §4.9).
- **Expiring clips.** On `cf campaign drain`/`end`, expire the campaign's unpublished candidates and clips (ClipState
  EXPIRED, ARCHITECTURE §2). `set_campaign_status` only changes the campaign row, because expiry touches
  publish/review state owned by D.
- **Retry of a FAILED source.** There is no command for this yet. A `cf source retry <id>` (FAILED → REGISTERED, with
  an events row) would let the operator re-run a source after fixing a link.
