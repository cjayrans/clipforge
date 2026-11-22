"""Pure functions that enforce docs/COMPLIANCE.md.

Every function here is side-effect free and imports only the standard library and `clipforge.schemas`, so it can be
type-checked with `mypy --strict` and tested without I/O. Callers (ingest, render, qa, publish, review) must call
these at the point of action; they are the last line of defence, not a substitute for DB constraints.

A change that weakens any rule here needs Carson's explicit approval in the PR description.

External facts used here (verified 2026-11-22):
- Caption limits per platform: https://docs.upload-post.com/resources/character-limits
  (TikTok 2,200 UTF-16 code units; Instagram 2,200 chars; YouTube title 100 chars, description 5,000 UTF-8 bytes;
  X 280 chars, weighted).
- `first_comment` and `<platform>_first_comment` exist in Upload-Post's upload API
  (https://docs.upload-post.com/api/upload-video). We never send either.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, time, tzinfo
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

from clipforge.schemas import (
    Approval,
    ApprovalDecision,
    Campaign,
    CampaignIn,
    CampaignStatus,
    Evening,
    Platform,
    RenderManifest,
    SourceHost,
)


class ComplianceError(ValueError):
    """Raised when an action would break a rule in docs/COMPLIANCE.md."""

    def __init__(self, invariant: int | None, message: str) -> None:
        self.invariant = invariant
        prefix = f"[compliance #{invariant}] " if invariant is not None else "[compliance] "
        super().__init__(prefix + message)


# --------------------------------------------------------------------------------------------------
# Constants (floors; config may be stricter, never looser)
# --------------------------------------------------------------------------------------------------

AD_PREFIX = "#ad"
AD_LABEL_MIN_END_S = 3.0
MIN_CLIP_S = 10.0
MAX_CLIP_S = 90.0
RENDER_WIDTH = 1080
RENDER_HEIGHT = 1920

POLITICAL_BLOCKED_PLATFORMS: frozenset[Platform] = frozenset({Platform.TIKTOK, Platform.X})

# Caption length limits per platform (see module docstring). YouTube's caption is the video description.
CAPTION_LIMITS: dict[Platform, int] = {
    Platform.TIKTOK: 2200,
    Platform.INSTAGRAM: 2200,
    Platform.YOUTUBE: 5000,
    Platform.X: 280,
}
YOUTUBE_TITLE_LIMIT = 100

# Payload keys that would automate engagement. Matching is on the normalized key (lowercase, "[]" removed); any key
# ending in "first_comment" (e.g. "tiktok_first_comment") is also forbidden.
FORBIDDEN_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {
        "first_comment",
        "comment",
        "comments",
        "auto_comment",
        "reply",
        "like",
        "likes",
        "auto_like",
        "follow",
        "follows",
        "auto_follow",
        "dm",
        "dms",
        "direct_message",
        "auto_dm",
    }
)

MEDIA_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v", ".mp3", ".m4a", ".wav"})

_YOUTUBE_DOMAINS = ("youtube.com", "youtu.be", "youtube-nocookie.com")
_KICK_DOMAINS = ("kick.com",)
_TWITCH_DOMAINS = ("twitch.tv",)
_GDRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")
_DROPBOX_DOMAINS = ("dropbox.com", "dropboxusercontent.com")
# Platforms and marketplaces we never download from, even as a "direct" link.
_REFUSED_DOMAINS = (
    "tiktok.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "facebook.com",
    "fb.watch",
    "contentrewards.com",
    "vyro.com",
    "vyro.app",
)


# --------------------------------------------------------------------------------------------------
# Invariant 1: sources
# --------------------------------------------------------------------------------------------------


def normalize_url(url: str) -> str:
    """Canonical form used to compare registered URLs: trimmed, scheme/host lowercased, fragment dropped."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_source_host(url: str) -> SourceHost:
    """Classify a source URL by host. Only GDRIVE, DROPBOX, DIRECT, INBOX (and TWITCH with permission) download."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme == "file":
        return SourceHost.INBOX if parts.netloc.lower() == "inbox" and parts.path not in ("", "/") else SourceHost.OTHER
    if scheme not in ("http", "https"):
        return SourceHost.OTHER
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return SourceHost.OTHER
    if _host_matches(host, _YOUTUBE_DOMAINS):
        return SourceHost.YOUTUBE
    if _host_matches(host, _KICK_DOMAINS):
        return SourceHost.KICK
    if _host_matches(host, _TWITCH_DOMAINS):
        return SourceHost.TWITCH
    if host in _GDRIVE_HOSTS:
        return SourceHost.GDRIVE
    if _host_matches(host, _DROPBOX_DOMAINS):
        return SourceHost.DROPBOX
    if _host_matches(host, _REFUSED_DOMAINS):
        return SourceHost.OTHER
    path = parts.path.lower()
    if any(path.endswith(ext) for ext in MEDIA_EXTENSIONS):
        return SourceHost.DIRECT
    return SourceHost.OTHER


def assert_registered_source(url: str, campaign: Campaign, registered_urls: Iterable[str]) -> SourceHost:
    """Invariant 1. Refuse unless `url` is registered on this active campaign and its host may be downloaded.

    `registered_urls` are the URLs of this campaign's `sources` rows. Returns the host kind so the downloader can
    pick a handler.
    """
    if campaign.status is not CampaignStatus.ACTIVE:
        raise ComplianceError(1, f"campaign {campaign.id} is {campaign.status.value}, not active")
    normalized = normalize_url(url)
    if normalized not in {normalize_url(u) for u in registered_urls}:
        raise ComplianceError(1, f"URL is not registered on campaign {campaign.id}: {normalized}")
    host = classify_source_host(normalized)
    if host is SourceHost.YOUTUBE:
        raise ComplianceError(1, "YouTube is never downloaded (YouTube terms), even when registered")
    if host is SourceHost.KICK:
        raise ComplianceError(1, "Kick is never downloaded, even when registered")
    if host is SourceHost.OTHER:
        raise ComplianceError(1, f"host not allowed for download: {normalized}")
    if host is SourceHost.TWITCH and not campaign.allow_stream_download:
        raise ComplianceError(1, f"campaign {campaign.id} does not allow stream downloads (allow_stream_download)")
    return host


# --------------------------------------------------------------------------------------------------
# Invariants 2 and 9: captions and platforms
# --------------------------------------------------------------------------------------------------

_URL_RE = re.compile(
    r"""(?ix)
    (?:\b(?:https?|ftp)://[^\s]+)                                   # scheme URLs
    | (?:\bwww\.[^\s]+)                                             # www. links
    | (?:(?<![@\w#])(?:[a-z0-9-]+\.)+                               # bare domains like twitch.tv/name
        (?:com|net|org|tv|gg|io|co|me|ly|app|xyz|live|link|be|uk|us|de|fr|ca|info|biz|stream|social)
        \b(?:/[^\s]*)?)
    """
)
_AD_TOKEN_RE = re.compile(r"^#ad(?![\w])[\s,.:;!|-]*", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t]+")


def contains_url(text: str) -> bool:
    return _URL_RE.search(text) is not None


def strip_urls(text: str) -> str:
    """Remove URLs (scheme, www. and bare-domain forms) and tidy the whitespace left behind."""
    stripped = _URL_RE.sub("", text)
    lines = [_WS_RE.sub(" ", line).strip() for line in stripped.splitlines()]
    return "\n".join(lines).strip()


def _strip_leading_ad(text: str) -> str:
    out = text.strip()
    while True:
        new = _AD_TOKEN_RE.sub("", out, count=1).strip()
        if new == out:
            return out
        out = new


def caption_length(text: str, platform: Platform) -> int:
    """Length as each platform counts it (conservative where the platform's rule is weighted)."""
    if platform is Platform.TIKTOK:
        return len(text.encode("utf-16-le")) // 2
    if platform is Platform.YOUTUBE:
        return len(text.encode("utf-8"))
    if platform is Platform.X:
        # X weights most non-Latin characters and emoji as 2. Count everything above U+10FF as 2 (conservative).
        return sum(2 if ord(ch) > 0x10FF else 1 for ch in text)
    return len(text)


def _has_tag(text: str, tag: str) -> bool:
    return re.search(rf"(?<![\w#]){re.escape(tag)}(?![\w])", text, re.IGNORECASE) is not None


def normalize_caption(text: str, platform: Platform, campaign: CampaignIn) -> str:
    """Invariants 2 and 9. Return the caption to publish on `platform`.

    Result: "#ad " + extra disclosure (campaign.disclosure_text without its leading #ad) + body + any missing
    required tags. X captions have URLs removed. If over the platform limit, the body is shortened at a word
    boundary; the prefix and tags are never cut.
    """
    extra = _strip_leading_ad(campaign.disclosure_text)
    body = _strip_leading_ad(text)
    if platform is Platform.X:
        extra = strip_urls(extra)
        body = strip_urls(body)
    tags = [t.strip() for t in campaign.required_tags if t.strip()]
    missing = [t for t in tags if not _has_tag(f"{extra} {body}", t)]
    # De-duplicate while keeping order.
    missing = list(dict.fromkeys(missing))

    def compose(b: str) -> str:
        parts = [AD_PREFIX, extra, b, " ".join(missing)]
        return " ".join(p for p in parts if p)

    if not (extra or body or missing):
        raise ComplianceError(2, "caption has nothing after the #ad disclosure")

    limit = CAPTION_LIMITS[platform]
    caption = compose(body)
    if caption_length(caption, platform) > limit:
        if caption_length(compose(""), platform) > limit:
            raise ComplianceError(2, f"disclosure and required tags alone exceed the {platform.value} limit")
        words = body.split(" ")
        while words and caption_length(compose(" ".join(words) + "…"), platform) > limit:
            words.pop()
        caption = compose(" ".join(words) + "…") if words else compose("")

    if not caption.startswith(AD_PREFIX + " "):  # pragma: no cover - guaranteed by construction
        raise ComplianceError(2, "caption must start with '#ad '")
    if platform is Platform.X and contains_url(caption):  # pragma: no cover - guaranteed by construction
        raise ComplianceError(9, "X caption contains a URL")
    return caption


def allowed_platforms(campaign: CampaignIn, enabled: Iterable[Platform] | None = None) -> list[Platform]:
    """Invariant 9. Platforms a clip of `campaign` may go to: campaign-allowed ∩ account-enabled, minus TikTok and X
    for political campaigns. Order follows the campaign's list."""
    enabled_set = set(enabled) if enabled is not None else set(Platform)
    out: list[Platform] = []
    for p in campaign.allowed_platforms:
        if p in enabled_set and p not in out and not (campaign.is_political and p in POLITICAL_BLOCKED_PLATFORMS):
            out.append(p)
    return out


def assert_platform_allowed(
    campaign: CampaignIn, platform: Platform, enabled: Iterable[Platform] | None = None
) -> None:
    """QA/publish gate (PRD F15, invariant 9): refuse a post to a platform the campaign doesn't allow."""
    if platform not in allowed_platforms(campaign, enabled):
        raise ComplianceError(9, f"platform {platform.value} is not allowed for campaign {campaign.name!r}")


# --------------------------------------------------------------------------------------------------
# Invariants 3 and 10: render manifest and duration
# --------------------------------------------------------------------------------------------------


def duration_problems(duration_s: float) -> list[str]:
    if not MIN_CLIP_S <= duration_s <= MAX_CLIP_S:
        return [f"duration {duration_s:.2f}s outside {MIN_CLIP_S:.0f}-{MAX_CLIP_S:.0f}s"]
    return []


def assert_duration_bounds(duration_s: float) -> None:
    """Invariant 10: clips are 10-90 s."""
    problems = duration_problems(duration_s)
    if problems:
        raise ComplianceError(10, problems[0])


def render_manifest_problems(manifest: RenderManifest) -> list[str]:
    """Every compliance problem with a render manifest (empty list = compliant). QA turns these into checks."""
    problems: list[str] = []
    if not manifest.has_ad_label:
        problems.append("no 'Ad' label")
    if manifest.ad_label_start_s > 0.0:
        problems.append(f"'Ad' label starts at {manifest.ad_label_start_s:.2f}s, must start at 0.0s")
    if manifest.ad_label_end_s < AD_LABEL_MIN_END_S:
        problems.append(f"'Ad' label ends at {manifest.ad_label_end_s:.2f}s, must last until >= {AD_LABEL_MIN_END_S}s")
    if not manifest.audio_source_only:
        problems.append("audio other than the source track is mapped")
    if (manifest.width, manifest.height) != (RENDER_WIDTH, RENDER_HEIGHT):
        problems.append(f"resolution {manifest.width}x{manifest.height}, expected {RENDER_WIDTH}x{RENDER_HEIGHT}")
    problems.extend(duration_problems(manifest.duration_s))
    return problems


def assert_render_manifest(manifest: RenderManifest) -> None:
    """Invariants 3 and 10: 'Ad' label from 0.0 s to >= 3.0 s, source audio only, 1080x1920, 10-90 s."""
    problems = render_manifest_problems(manifest)
    if problems:
        raise ComplianceError(3, "; ".join(problems))


# --------------------------------------------------------------------------------------------------
# Invariants 5, 6, 7, 8, 11: publishing gates
# --------------------------------------------------------------------------------------------------


def _require_aware(dt: datetime, what: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ComplianceError(None, f"{what} must be timezone-aware")


def assert_human_approved(approvals: Sequence[Approval], rendered_at: datetime) -> Approval:
    """Invariant 5. The latest operator decision made after the clip's latest render must be `approve`.

    Returns that approval. Approvals made before `rendered_at` are stale (a re-render needs a fresh approval).
    """
    _require_aware(rendered_at, "rendered_at")
    fresh: list[Approval] = []
    for a in approvals:
        _require_aware(a.decided_at, "approval.decided_at")
        if a.actor == "operator" and a.decided_at > rendered_at:
            fresh.append(a)
    if not fresh:
        raise ComplianceError(5, "no operator approval after the clip's latest render")
    latest = max(fresh, key=lambda a: a.decided_at)
    if latest.decision is not ApprovalDecision.APPROVE:
        raise ComplianceError(5, f"latest operator decision is {latest.decision.value}")
    return latest


def assert_evening_armed(evening: Evening | None) -> None:
    """Invariant 6: nothing publishes on an evening that was not armed (or was skipped)."""
    if evening is None or not evening.armed:
        raise ComplianceError(6, "evening is not armed")


class ExistingPost(NamedTuple):
    clip_id: int
    sha256: str
    platform: Platform


def assert_unique_post(clip_id: int, sha256: str, platform: Platform, existing: Iterable[ExistingPost]) -> None:
    """Invariant 7: one post per clip per platform, and the same clip file never goes to a platform twice."""
    for post in existing:
        if post.platform is not platform:
            continue
        if post.clip_id == clip_id:
            raise ComplianceError(7, f"clip {clip_id} already has a {platform.value} post")
        if post.sha256 == sha256:
            raise ComplianceError(7, f"this file was already posted to {platform.value} (clip {post.clip_id})")


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("[]", "")


def assert_no_engagement_fields(payload: Mapping[str, object]) -> None:
    """Invariant 8: no engagement fields (first_comment, likes, follows, DMs ...) anywhere in a request payload."""

    def walk(obj: object, path: str) -> None:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                key = _normalize_key(str(k))
                if key in FORBIDDEN_PAYLOAD_FIELDS or key.endswith("first_comment"):
                    raise ComplianceError(8, f"forbidden engagement field in payload: {path}{k}")
                walk(v, f"{path}{k}.")
        elif isinstance(obj, list | tuple):
            for i, item in enumerate(obj):
                walk(item, f"{path}{i}.")

    walk(payload, "")


def assert_slot_in_window(slot_at: datetime, window_start: time, window_end: time, tz: tzinfo) -> None:
    """Invariant 11: a publish slot falls inside the operator window [start, end) in the operator's timezone."""
    _require_aware(slot_at, "slot_at")
    local = slot_at.astimezone(tz).time().replace(tzinfo=None)
    if not window_start <= local < window_end:
        raise ComplianceError(11, f"slot {local:%H:%M} outside operator window {window_start:%H:%M}-{window_end:%H:%M}")
