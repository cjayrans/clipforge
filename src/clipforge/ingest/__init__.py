"""Campaign registry, source registration and download.

Owner: Phase 1 worktree A (`feat/ingest-transcribe`). See docs/ARCHITECTURE.md §4.1.

Entry points other code already expects (keep these names):
- `ingest.registry.add_campaign_from_json(session, settings, path) -> CampaignRow`   (`cf campaign add --from-json`)
- `ingest.registry.list_campaigns(session) -> list[CampaignRow]`                       (`cf campaign list`)
- `ingest.registry.set_campaign_status(session, campaign_id, status) -> CampaignRow`  (`cf campaign drain/end`)
- `ingest.registry.check_campaign(session, settings, campaign_id) -> list[schemas.QACheck]` (`cf campaign check`)
- `ingest.registry.add_source(session, settings, campaign_id, url) -> list[SourceRow]`  (`cf source add`)
- `ingest.download.download_source(session, settings, source: SourceRow) -> SourceRow`
  (must call `compliance.assert_registered_source`; see tests/compliance/test_inv01_sources.py)
"""
