"""Find recent Twitch VODs of a campaign's stream channels and PROPOSE them to the operator (moments-v2, PRD F1).

Nothing here registers or downloads anything. The nightly job (`discover_vods`) writes `vod_proposals` rows; the bot
sends each one with Add / Dismiss buttons; only the operator's Add tap registers the URL as a source through
`registry.add_source`, the same path as `cf source add` (COMPLIANCE #1: only registered URLs are ever downloaded).

Which campaigns: active, `auto_propose_vods` true, `allow_stream_download` true (set from the brief), with
`stream_channels` and `recent_vod_days` (the brief's "recent VODs only" rule). Per campaign per night at most
`MAX_PROPOSALS_PER_NIGHT` VODs, newest first, skipping VODs older than `recent_vod_days`, VODs of a stream that is
still live, and URLs already registered or already proposed.

Twitch Helix API (official; verified 2026-11-26 at https://dev.twitch.tv/docs/api/reference/videos, .../users,
.../streams and https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-grant-flow):
- App access token: POST https://id.twitch.tv/oauth2/token (form: client_id, client_secret,
  grant_type=client_credentials) -> access_token, expires_in, token_type.
- Every call sends `Authorization: Bearer <token>` and `Client-Id: <client id>`; app tokens are accepted.
- GET /helix/users?login=<login> -> data[].id, login.
- GET /helix/streams?user_login=<login> -> data[].id (stream id), type "live".
- GET /helix/videos?user_id=<id>&type=archive&sort=time&first=<n> -> data[]: id, stream_id, user_login, title,
  created_at (RFC3339 UTC), url, duration ("1h0m5s"), type.
The docs do not say when an archive VOD appears during a live stream, so a VOD whose stream_id matches a live stream
is skipped (field semantics, not a documented rule).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from clipforge import compliance
from clipforge.config import Settings
from clipforge.db import CampaignRow, SourceRow, VodProposalRow, record_event, utcnow
from clipforge.ingest import alerts
from clipforge.schemas import CampaignStatus

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.twitch.tv/helix"
TIMEOUT_S = 20.0
MAX_ATTEMPTS = 3
VIDEOS_PER_CHANNEL = 20
MAX_PROPOSALS_PER_NIGHT = 2
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class HelixError(RuntimeError):
    """A Twitch API call failed (after retries) or returned something unusable."""


def parse_duration(text: str) -> float | None:
    """'1h0m5s' -> 3605.0; None when the format is unknown."""
    m = _DURATION_RE.match(text.strip()) if text else None
    if m is None or not any(m.groups()):
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return float(h * 3600 + mi * 60 + s)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and (
        exc.response.status_code == 429 or exc.response.status_code >= 500
    )


@dataclass(frozen=True)
class Vod:
    video_id: str
    url: str
    login: str
    title: str
    created_at: datetime
    duration_s: float | None
    stream_id: str | None


class HelixClient:
    """The one Twitch Helix client: app token, timeouts, bounded retries. No cost (the API is free)."""

    def __init__(
        self, client_id: str, client_secret: str, *, http: httpx.Client | None = None, backoff_s: float = 1.0
    ) -> None:
        if not client_id or not client_secret:
            raise HelixError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are needed to look up VODs")
        self._id = client_id
        self._secret = client_secret
        self._http = http or httpx.Client(timeout=TIMEOUT_S)
        self._token: str | None = None
        self._backoff = backoff_s

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> HelixClient | None:
        s = settings.secrets
        if s.twitch_client_id is None or s.twitch_client_secret is None:
            return None
        return cls(s.twitch_client_id.get_secret_value(), s.twitch_client_secret.get_secret_value(), **kwargs)

    def _send(self, method: str, url: str, **kwargs: Any) -> Any:
        last: Exception | None = None
        try:
            for attempt in Retrying(
                retry=retry_if_exception(_retryable),
                stop=stop_after_attempt(MAX_ATTEMPTS),
                wait=wait_exponential(multiplier=self._backoff, max=10),
                reraise=True,
            ):
                with attempt:
                    resp = self._http.request(method, url, **kwargs)
                    resp.raise_for_status()
                    return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
        status = getattr(getattr(last, "response", None), "status_code", None)
        raise HelixError(f"Twitch {method} {url.split('?')[0]} failed: {type(last).__name__} {status or ''}".strip())

    def _token_value(self) -> str:
        if self._token is None:
            data = self._send(
                "POST",
                TOKEN_URL,
                data={"client_id": self._id, "client_secret": self._secret, "grant_type": "client_credentials"},
            )
            token = data.get("access_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise HelixError("Twitch token response has no access_token")
            self._token = token
        return self._token

    def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self._token_value()}", "Client-Id": self._id}
        data = self._send("GET", f"{API_BASE}/{path}", params=params, headers=headers)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise HelixError(f"Twitch {path} response has no data list")
        return [x for x in items if isinstance(x, dict)]

    def user_id(self, login: str) -> str | None:
        users = self._get("users", {"login": login})
        return str(users[0]["id"]) if users and users[0].get("id") else None

    def live_stream_ids(self, login: str) -> set[str]:
        return {str(s["id"]) for s in self._get("streams", {"user_login": login}) if s.get("id")}

    def archive_vods(self, user_id: str, first: int = VIDEOS_PER_CHANNEL) -> list[Vod]:
        out: list[Vod] = []
        params = {"user_id": user_id, "type": "archive", "sort": "time", "first": first}
        for v in self._get("videos", params):
            try:
                created = datetime.fromisoformat(str(v["created_at"]).replace("Z", "+00:00"))
                out.append(
                    Vod(
                        video_id=str(v["id"]),
                        url=str(v.get("url") or f"https://www.twitch.tv/videos/{v['id']}"),
                        login=str(v.get("user_login") or "").lower(),
                        title=str(v.get("title") or ""),
                        created_at=created,
                        duration_s=parse_duration(str(v.get("duration") or "")),
                        stream_id=str(v["stream_id"]) if v.get("stream_id") else None,
                    )
                )
            except (KeyError, ValueError):
                continue
        return out


def eligible_campaigns(session: Session) -> list[CampaignRow]:
    return list(
        session.scalars(
            select(CampaignRow).where(
                CampaignRow.status == CampaignStatus.ACTIVE,
                CampaignRow.auto_propose_vods.is_(True),
                CampaignRow.allow_stream_download.is_(True),
            )
        )
    )


def discover_vods(session: Session, settings: Settings, *, client: HelixClient, now: datetime | None = None) -> int:
    """Write up to MAX_PROPOSALS_PER_NIGHT new pending proposals per eligible campaign. Returns how many."""
    now = now or utcnow()
    made = 0
    for camp in eligible_campaigns(session):
        if not camp.stream_channels or not camp.recent_vod_days:
            continue
        cutoff = now - timedelta(days=camp.recent_vod_days)
        registered = set(session.scalars(select(SourceRow.url).where(SourceRow.campaign_id == camp.id)))
        proposed = set(session.scalars(select(VodProposalRow.url).where(VodProposalRow.campaign_id == camp.id)))
        found: list[Vod] = []
        for login in camp.stream_channels:
            try:
                uid = client.user_id(str(login))
                if uid is None:
                    continue
                live = client.live_stream_ids(str(login))
                vods = client.archive_vods(uid)
            except HelixError as exc:
                alerts.alert_operator(
                    session, alerts.VOD_DISCOVERY_FAILED, f"campaign:{camp.id}", f"VOD lookup for {login}: {exc}"
                )
                continue
            for v in vods:
                url = compliance.normalize_url(v.url)
                if v.created_at < cutoff or (v.stream_id and v.stream_id in live):
                    continue
                if v.login and v.login != str(login).lower():
                    continue
                if url in registered or url in proposed:
                    continue
                found.append(Vod(v.video_id, url, v.login or str(login), v.title, v.created_at, v.duration_s,
                                 v.stream_id))  # fmt: skip
        found.sort(key=lambda v: v.created_at, reverse=True)
        for v in found[:MAX_PROPOSALS_PER_NIGHT]:
            row = VodProposalRow(
                campaign_id=camp.id,
                url=v.url,
                video_id=v.video_id,
                channel=v.login,
                title=v.title[:500],
                vod_created_at=v.created_at,
                duration_s=v.duration_s,
                proposed_at=now,
            )
            session.add(row)
            session.flush()
            proposed.add(v.url)
            record_event(session, "vod_proposal", row.id, None, "pending", detail=f"campaign {camp.id}: {v.url}")
            made += 1
    return made


def decide(session: Session, settings: Settings, proposal_id: int, *, add: bool, now: datetime) -> str:
    """The operator's Add / Dismiss. Add registers the URL through `registry.add_source` (compliance checks there)."""
    from clipforge.ingest.registry import add_source

    row = session.get(VodProposalRow, proposal_id)
    if row is None:
        return f"VOD proposal {proposal_id} not found."
    if row.decision != "pending":
        return f"VOD proposal {proposal_id} was already {row.decision}."
    camp = session.get(CampaignRow, row.campaign_id)
    too_old = (
        camp is not None
        and camp.recent_vod_days
        and row.vod_created_at is not None
        and row.vod_created_at < now - timedelta(days=camp.recent_vod_days)  # the brief's "recent VODs only" rule
    )
    if add and too_old and camp is not None:
        row.decision, row.decided_at = "expired", now
        record_event(session, "vod_proposal", row.id, "pending", "expired", actor="operator",
                     detail=f"older than {camp.recent_vod_days} days")  # fmt: skip
        return f"Not registered: {row.url} is older than the campaign's {camp.recent_vod_days}-day window."
    if not add:
        row.decision, row.decided_at = "dismissed", now
        record_event(session, "vod_proposal", row.id, "pending", "dismissed", actor="operator")
        return f"Dismissed {row.url}."
    sources = add_source(session, settings, row.campaign_id, row.url)
    if not sources:
        return f"Not registered: {row.url} was refused (see the alert)."
    row.decision, row.decided_at, row.source_id = "added", now, sources[0].id
    record_event(
        session, "vod_proposal", row.id, "pending", "added", actor="operator", detail=f"source {sources[0].id}"
    )
    return f"Registered {row.url} as source {sources[0].id}; tonight's run picks it up."


def proposal_text(row: VodProposalRow) -> str:
    hours = f"{row.duration_s / 3600:.1f} h" if row.duration_s else "length unknown"
    when = f"{row.vod_created_at:%d %b %H:%M} UTC" if row.vod_created_at else "date unknown"
    return f"New VOD from {row.channel} ({when}, {hours}):\n{row.title or '(no title)'}\n{row.url}\nAdd it as a source?"
