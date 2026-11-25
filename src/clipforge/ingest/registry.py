"""Campaign registry and source registration (ARCHITECTURE §4.1, PRD F1/F2/F15/F17).

These functions back `cf campaign ...` and `cf source add`. The CLI (Phase 0) does not print return values, so each
function prints a short summary itself and also returns its rows for programmatic callers (pipeline, tests).

Errors the operator can fix (bad JSON, unknown campaign id, illegal status change) raise `RegistryError`, a
click `ClickException`, so `cf` prints `Error: <message>` and exits 1 instead of a traceback.

A refused source URL is not an exception: `add_source` writes an operator alert, prints the reason and returns an
empty list, so the alert is committed with the session.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge import compliance
from clipforge.config import Settings
from clipforge.db import CampaignRow, SourceRow, record_event
from clipforge.ingest import alerts
from clipforge.ingest.folders import FolderListError, list_gdrive_folder
from clipforge.ingest.models import campaign_from_row, campaign_row_values, set_source_status
from clipforge.rank.profile import ProfileError, load_profile
from clipforge.render import campaign_overlay
from clipforge.render.assets import project_root
from clipforge.schemas import CampaignIn, CampaignStatus, Platform, QACheck, SourceHost, SourceStatus, slugify

log = logging.getLogger(__name__)
console = Console(highlight=False)

# Campaign status changes the operator may make. ENDED is terminal; DRAINED -> ACTIVE covers a budget top-up.
CAMPAIGN_STATUS_TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.ACTIVE: frozenset({CampaignStatus.DRAINED, CampaignStatus.ENDED}),
    CampaignStatus.DRAINED: frozenset({CampaignStatus.ACTIVE, CampaignStatus.ENDED}),
    CampaignStatus.ENDED: frozenset(),
}

# Platforms every campaign must accept (docs/CAMPAIGNS.md screening rule 3).
REQUIRED_PLATFORMS: tuple[Platform, ...] = (Platform.TIKTOK, Platform.INSTAGRAM)


# The ClickException class typer handles (typer >= 0.20 vendors its own click), so `cf` prints `Error: ...`, exits 1.
_ClickException: type[Exception] = next(c for c in typer.BadParameter.__mro__ if c.__name__ == "ClickException")


class RegistryError(_ClickException):  # type: ignore[valid-type,misc]
    """An operator-fixable registry problem; `cf` prints it as `Error: ...` and exits 1."""


# --------------------------------------------------------------------------------------------------
# Campaigns
# --------------------------------------------------------------------------------------------------


def load_campaign_json(path: Path | str) -> CampaignIn:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"campaign file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{p} is not valid JSON: {exc}") from exc
    try:
        return CampaignIn.model_validate(raw)
    except ValidationError as exc:
        lines = [f"  {'.'.join(str(x) for x in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]
        raise RegistryError(f"{p} does not match schemas.CampaignIn:\n" + "\n".join(lines)) from exc


def validate_campaign_setup(campaign: CampaignIn, settings: Settings) -> None:
    """Checks that need config or files, run before a campaign is saved, so a bad JSON fails at `cf campaign add`
    instead of at render or publish time.

    - Invariant 9: X captions carry no URL, so a campaign whose required caption text contains a URL (a channel
      link) cannot list X.
    - Invariant 3 / overlay: the overlay file exists, is an RGBA PNG under campaigns/assets/, and its visible part
      misses the captions, the hook, the platform UI zones, and leaves a spot for the "Ad" label.
    - Moment profile: the named file in config/moment_profiles/ exists and is valid.
    """
    if campaign.moment_profile is not None:
        try:
            profile = load_profile(campaign.moment_profile)
        except ProfileError as exc:
            raise RegistryError(f"moment_profile: {exc}") from exc
        console.print(f"moment profile {profile.name} ({profile.weights_version}): {', '.join(profile.archetype_ids)}")
    url_tags = [t for t in campaign.required_tags if compliance.contains_url(t)]
    if url_tags and Platform.X in campaign.allowed_platforms:
        raise RegistryError(
            f"required caption text {url_tags} contains a URL, which X captions may not carry (invariant 9): "
            "remove x from allowed_platforms"
        )
    if campaign.overlay is not None:
        try:
            placement = campaign_overlay.resolve(
                campaign.overlay, project_root(), settings.app.styles.values(), settings.app.render.ad_label_text
            )
        except campaign_overlay.OverlayPlacementError as exc:
            raise RegistryError(f"overlay: {exc}") from exc
        ad = campaign_overlay.place_ad_label(settings.app.render.ad_label_text, placement, settings.app.styles.values())
        o = placement.opaque_rect
        console.print(
            f"overlay {campaign.overlay.path}: visible area x {o.x0}-{o.x1}, y {o.y0}-{o.y1} "
            f"(clear of captions, hook and platform UI); 'Ad' label at ({ad.x}, {ad.y})"
        )


def upsert_campaign(session: Session, campaign: CampaignIn) -> tuple[CampaignRow, bool]:
    """Insert, or update the campaign with the same slug. Status and created_at are never changed here.

    Returns (row, created).
    """
    slug = slugify(campaign.name)
    row = session.scalar(select(CampaignRow).where(CampaignRow.slug == slug))
    values = campaign_row_values(campaign)
    if row is None:
        row = CampaignRow(slug=slug, status=CampaignStatus.ACTIVE, **values)
        session.add(row)
        session.flush()
        record_event(session, "campaign", row.id, None, CampaignStatus.ACTIVE.value, actor="operator", detail="added")
        return row, True
    for key, value in values.items():
        setattr(row, key, value)
    session.flush()
    record_event(session, "campaign", row.id, row.status.value, row.status.value, actor="operator", detail="updated")
    return row, False


def add_campaign_from_json(session: Session, settings: Settings, path: Path | str) -> CampaignRow:
    """`cf campaign add --from-json`: validate against CampaignIn, upsert by slug, register its `source_urls`."""
    campaign = load_campaign_json(path)
    validate_campaign_setup(campaign, settings)
    row, created = upsert_campaign(session, campaign)
    verb = "registered" if created else "updated"
    console.print(f"{verb} campaign {row.id}: {row.name} [{row.status.value}] (inbox/{row.slug}/)")
    if row.status is not CampaignStatus.ACTIVE and campaign.source_urls:
        console.print(f"[yellow]campaign is {row.status.value}: source_urls not registered[/yellow]")
        return row
    for url in campaign.source_urls:
        add_source(session, settings, row.id, str(url))
    return row


def list_campaigns(session: Session, settings: Settings | None = None) -> list[CampaignRow]:
    """`cf campaign list`. `settings` is accepted because `cf` passes it; it is not used."""
    rows = list(session.scalars(select(CampaignRow).order_by(CampaignRow.id)))
    counts: dict[int, int] = {}
    for campaign_id in session.scalars(select(SourceRow.campaign_id)):
        counts[campaign_id] = counts.get(campaign_id, 0) + 1
    table = Table("id", "name", "status", "platforms", "cpm", "budget left", "stream dl", "sources")
    for r in rows:
        table.add_row(
            str(r.id),
            r.name,
            r.status.value,
            ",".join(r.allowed_platforms),
            f"${r.cpm_usd:.2f}",
            "-" if r.budget_remaining_usd is None else f"${r.budget_remaining_usd:,.0f}",
            "yes" if r.allow_stream_download else "no",
            str(counts.get(r.id, 0)),
        )
    console.print(table if rows else "no campaigns registered")
    return rows


def get_campaign(session: Session, campaign_id: int) -> CampaignRow:
    row = session.get(CampaignRow, campaign_id)
    if row is None:
        raise RegistryError(f"no campaign with id {campaign_id}")
    return row


def set_campaign_status(
    session: Session, settings: Settings | None, campaign_id: int, status: CampaignStatus
) -> CampaignRow:
    """`cf campaign drain` / `cf campaign end`. Writes an events row.

    Draining or ending a campaign also expires its unpublished candidates and clips (ARCHITECTURE §2, CR-A5;
    `publish.dispatcher.expire_campaign_clips`). Expiry is permanent: re-activating a drained campaign does not bring
    them back.
    """
    status = CampaignStatus(status)
    row = get_campaign(session, campaign_id)
    old = row.status
    if old is status:
        console.print(f"campaign {row.id} is already {status.value}")
        return row
    if status not in CAMPAIGN_STATUS_TRANSITIONS[old]:
        raise RegistryError(f"campaign {row.id} is {old.value}; it cannot become {status.value}")
    row.status = status
    session.flush()
    record_event(session, "campaign", row.id, old.value, status.value, actor="operator", detail=f"cf campaign {status}")
    console.print(f"campaign {row.id} ({row.name}): {old.value} -> {status.value}")
    if status in (CampaignStatus.DRAINED, CampaignStatus.ENDED):
        from clipforge.publish.dispatcher import expire_campaign_clips  # lazy: publish imports the HTTP client

        expired = expire_campaign_clips(session, row.id, f"campaign {status.value}", actor="operator")
        if expired:
            console.print(f"expired {expired} unpublished candidate(s)/clip(s) of campaign {row.id}")
    return row


# --------------------------------------------------------------------------------------------------
# Campaign filter (`cf campaign check`, docs/CAMPAIGNS.md screening rules)
# --------------------------------------------------------------------------------------------------


def views_to_min_payout(min_payout_usd: float, cpm_usd: float) -> float | None:
    """Views needed to reach the minimum payout at `cpm_usd` per 1,000 views (None when the rate is 0)."""
    if min_payout_usd <= 0:
        return 0.0
    if cpm_usd <= 0:
        return None
    return min_payout_usd / cpm_usd * 1000


def campaign_checks(campaign: CampaignIn, settings: Settings) -> list[QACheck]:
    """Pure: apply `config.campaign_filter` to a campaign. One QACheck per rule."""
    f = settings.app.campaign_filter
    checks: list[QACheck] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(QACheck(name=name, passed=passed, detail=detail))

    if campaign.approval_rate_pct is None:
        add("approval_rate", True, "not shown on the campaign (rule skipped)")
    else:
        add(
            "approval_rate",
            campaign.approval_rate_pct >= f.min_approval_rate_pct,
            f"{campaign.approval_rate_pct:.0f}% (min {f.min_approval_rate_pct:.0f}%)",
        )

    budget = campaign.budget_remaining_usd
    if budget is None:
        add("budget_remaining", False, "budget_remaining_usd not recorded")
    else:
        add(
            "budget_remaining",
            budget >= f.min_budget_remaining_usd,
            f"${budget:,.0f} (min ${f.min_budget_remaining_usd:,.0f})",
        )
        if campaign.per_clip_cap_usd:
            ratio = budget / campaign.per_clip_cap_usd
            add(
                "budget_to_cap_ratio",
                ratio >= f.min_budget_to_cap_ratio,
                f"{ratio:.1f}x the ${campaign.per_clip_cap_usd:,.0f} per-clip cap (min {f.min_budget_to_cap_ratio:g}x)",
            )
        else:
            add("budget_to_cap_ratio", True, "no per-clip cap (rule skipped)")

    for platform in REQUIRED_PLATFORMS:
        if platform not in campaign.allowed_platforms:
            continue
        views = views_to_min_payout(campaign.min_payout_for(platform), campaign.cpm_for(platform))
        if views is None:
            add(f"views_to_min_payout_{platform.value}", False, f"no paid rate on {platform.value}")
        else:
            add(
                f"views_to_min_payout_{platform.value}",
                views <= f.max_views_to_min_payout,
                f"{views:,.0f} views (max {f.max_views_to_min_payout:,})",
            )

    if campaign.participants is None:
        add("participants", True, "not recorded (rule skipped)")
    else:
        add(
            "participants",
            campaign.participants < f.max_participants,
            f"{campaign.participants} clippers (must be under {f.max_participants})",
        )

    add(
        "footage_mode",
        campaign.footage_mode in f.allowed_footage_modes,
        f"{campaign.footage_mode} (allowed: {', '.join(f.allowed_footage_modes)})",
    )
    missing = [p.value for p in REQUIRED_PLATFORMS if p not in campaign.allowed_platforms]
    add("accepts_tiktok_instagram", not missing, "ok" if not missing else f"missing {', '.join(missing)}")
    add("not_political", not campaign.is_political, "political" if campaign.is_political else "ok")
    return checks


def check_campaign(session: Session, settings: Settings, campaign_id: int) -> list[QACheck]:
    """`cf campaign check <id>`: print the filter result per rule and return the checks."""
    row = get_campaign(session, campaign_id)
    checks = campaign_checks(campaign_from_row(row), settings)
    table = Table("check", "result", "detail")
    for c in checks:
        table.add_row(c.name, "[green]pass[/green]" if c.passed else "[red]FAIL[/red]", c.detail)
    console.print(table)
    passed = all(c.passed for c in checks)
    console.print(f"campaign {row.id} ({row.name}): " + ("[green]PASS[/green]" if passed else "[red]FAIL[/red]"))
    return checks


# --------------------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------------------

FolderLister = Callable[[str], Sequence[tuple[str, str]]]
"""(folder_url) -> [(file_url, relative_path), ...]; see ingest.folders."""


def is_gdrive_folder(url: str) -> bool:
    parts = urlsplit(url)
    return "/folders/" in parts.path or parts.path.rstrip("/").endswith("/folderview")


def is_dropbox_folder(url: str) -> bool:
    path = urlsplit(url).path
    return path.startswith(("/scl/fo/", "/sh/"))


def _refuse(session: Session, kind: str, campaign_id: int, url: str, reason: str) -> list[SourceRow]:
    alerts.alert_operator(session, kind, f"campaign:{campaign_id}", f"source refused: {url} ({reason})")
    console.print(f"[red]refused[/red] {url}: {reason}")
    return []


def refusal_reason(campaign: CampaignRow, url: str) -> tuple[str, str] | None:
    """(alert kind, reason) if `url` must not be registered on `campaign`, else None. Pure apart from reading row."""
    if campaign.status is not CampaignStatus.ACTIVE:
        return alerts.CAMPAIGN_INACTIVE, f"campaign {campaign.id} is {campaign.status.value}"
    host = compliance.classify_source_host(url)
    if host is SourceHost.YOUTUBE:
        return alerts.HOST_REFUSED, "YouTube is never downloaded (YouTube terms), even when a campaign allows clipping"
    if host is SourceHost.KICK:
        return alerts.HOST_REFUSED, "Kick is never downloaded"
    if host is SourceHost.OTHER:
        return alerts.HOST_REFUSED, (
            "host not allowed: only Google Drive, Dropbox, direct media links, inbox/ files and (with permission) "
            "Twitch"
        )
    if host is SourceHost.TWITCH and not campaign.allow_stream_download:
        return alerts.HOST_REFUSED, (
            "Twitch needs allow_stream_download=true on the campaign, set only when its rules allow downloading"
        )
    if host is SourceHost.INBOX:
        return alerts.HOST_REFUSED, "inbox files are registered by the inbox scan, not by URL"
    if host is SourceHost.DROPBOX and is_dropbox_folder(url):
        return alerts.HOST_REFUSED, (
            "Dropbox folder links cannot be listed without a Dropbox API token; register each file's own link, "
            f"or download the files and drop them in inbox/{campaign.slug}/"
        )
    return None


def register_source(
    session: Session, campaign: CampaignRow, url: str, *, folder_url: str | None = None
) -> tuple[SourceRow, bool]:
    """Insert one sources row (normalized URL), or return the existing one. Returns (row, created).

    Callers must have checked `refusal_reason` first.
    """
    normalized = compliance.normalize_url(url)
    existing = session.scalar(
        select(SourceRow).where(SourceRow.campaign_id == campaign.id, SourceRow.url == normalized)
    )
    if existing is not None:
        return existing, False
    row = SourceRow(campaign_id=campaign.id, url=normalized, folder_url=folder_url)
    session.add(row)
    session.flush()
    record_event(
        session,
        "source",
        row.id,
        None,
        row.status.value,
        actor="operator",
        detail=f"registered on campaign {campaign.id}"
        + (f" from folder {alerts.redact_urls(folder_url)}" if folder_url else ""),
    )
    return row, True


def add_source(
    session: Session,
    settings: Settings,
    campaign_id: int,
    url: str,
    *,
    folder_lister: FolderLister | None = None,
) -> list[SourceRow]:
    """`cf source add <campaign_id> <url>`. Register a footage URL, or every media file of a Drive folder link.

    Returns the registered rows (existing rows on a repeat). An empty list means the URL was refused; an operator
    alert has been written.
    """
    campaign = get_campaign(session, campaign_id)
    normalized = compliance.normalize_url(url)
    refusal = refusal_reason(campaign, normalized)
    if refusal is not None:
        return _refuse(session, refusal[0], campaign.id, normalized, refusal[1])

    if compliance.classify_source_host(normalized) is SourceHost.GDRIVE and is_gdrive_folder(normalized):
        return _add_gdrive_folder(session, campaign, normalized, folder_lister)

    row, created = register_source(session, campaign, normalized)
    console.print(f"{'registered' if created else 'already registered'}: source {row.id} {row.url}")
    return [row]


def _add_gdrive_folder(
    session: Session, campaign: CampaignRow, folder_url: str, folder_lister: FolderLister | None
) -> list[SourceRow]:
    lister = folder_lister or list_gdrive_folder
    try:
        files = lister(folder_url)
    except FolderListError as exc:
        return _refuse(session, alerts.DOWNLOAD_FAILED, campaign.id, folder_url, f"could not list folder: {exc}")
    rows: list[SourceRow] = []
    for file_url, rel_path in files:
        row, created = register_source(session, campaign, file_url, folder_url=folder_url)
        console.print(f"{'registered' if created else 'already registered'}: source {row.id} {rel_path}")
        rows.append(row)
    if not rows:
        console.print(f"[yellow]no media files found in {folder_url}[/yellow]")
    return rows


def retry_source(session: Session, settings: Settings | None, source_id: int) -> SourceRow:
    """`cf source retry <id>`: put a FAILED source back to REGISTERED so the next nightly run downloads it again
    (CR-A5), e.g. after the operator fixed a link. Writes an events row.

    Nothing is downloaded here, and the downloader re-checks invariant 1 every time, so a refused host fails again.
    A retried `no_speech` source is downloaded and transcribed again, which costs API money.
    """
    source = session.get(SourceRow, source_id)
    if source is None:
        raise RegistryError(f"no source with id {source_id}")
    if source.status is not SourceStatus.FAILED:
        raise RegistryError(f"source {source.id} is {source.status.value}; only failed sources can be retried")
    campaign = get_campaign(session, source.campaign_id)
    if campaign.status is not CampaignStatus.ACTIVE:
        raise RegistryError(f"campaign {campaign.id} is {campaign.status.value}; activate it before retrying sources")
    reason = source.fail_reason or "unknown"
    set_source_status(session, source, SourceStatus.REGISTERED, actor="operator", detail=f"retry after {reason}")
    console.print(f"source {source.id}: failed ({reason}) -> registered; the next nightly run downloads it again")
    if reason == "host_refused":
        console.print(
            "[yellow]this host was refused (COMPLIANCE #1); it will be refused again unless the link changed[/yellow]"
        )
    elif reason == "no_speech":
        console.print("[yellow]a no-speech source will be transcribed again (paid) and will likely fail again[/yellow]")
    return source
