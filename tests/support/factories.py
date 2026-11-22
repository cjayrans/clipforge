"""Row factories for tests. Every factory takes overrides and flushes so the row has an id.

Approvals made here are test fixtures against mocked services only; never use them against live APIs.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from clipforge.db import (
    ApprovalRow,
    CampaignRow,
    CandidateRow,
    ClipRow,
    EveningRow,
    PostRow,
    SourceRow,
    SubmissionRow,
)
from clipforge.schemas import ApprovalDecision, CampaignIn, CampaignStatus, Platform, slugify

_seq = itertools.count(1)


def campaign_in(**overrides: Any) -> CampaignIn:
    n = next(_seq)
    data: dict[str, Any] = {
        "marketplace": "content_rewards",
        "name": f"Test Campaign {n}",
        "url": f"https://contentrewards.com/c/test-{n}",
        "submission_url": f"https://contentrewards.com/c/test-{n}/submit",
        "cpm_usd": 1.0,
        "allowed_platforms": ["tiktok", "instagram"],
    }
    data.update(overrides)
    return CampaignIn.model_validate(data)


class Factory:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _add(self, row: Any) -> Any:
        self.session.add(row)
        self.session.flush()
        return row

    def campaign(self, *, status: str = "active", **overrides: Any) -> CampaignRow:
        c = campaign_in(**overrides)
        data = c.model_dump(mode="json")
        data["platform_marketplace"] = data.pop("marketplace")
        return self._add(CampaignRow(slug=slugify(c.name), status=CampaignStatus(status), **data))

    def source(self, campaign: CampaignRow | None = None, **overrides: Any) -> SourceRow:
        campaign = campaign or self.campaign()
        n = next(_seq)
        data: dict[str, Any] = {"url": f"https://cdn.example.com/footage-{n}.mp4"}
        data.update(overrides)
        return self._add(SourceRow(campaign_id=campaign.id, **data))

    def candidate(self, source: SourceRow | None = None, **overrides: Any) -> CandidateRow:
        source = source or self.source()
        data: dict[str, Any] = {
            "start_s": 10.0,
            "end_s": 45.0,
            "score": 80,
            "standalone": True,
            "brief_ok": True,
            "reasons": "test",
            "prompt_version": "test-v1",
        }
        data.update(overrides)
        return self._add(CandidateRow(source_id=source.id, **data))

    def clip(self, candidate: CandidateRow | None = None, **overrides: Any) -> ClipRow:
        candidate = candidate or self.candidate()
        n = next(_seq)
        data: dict[str, Any] = {
            "account_set": "gaming-1",
            "style_preset": "bold-yellow",
            "path": f"/tmp/clip-{n}.mp4",
            "duration_s": 35.0,
            "sha256": hashlib.sha256(f"clip-{n}".encode()).hexdigest(),
            "rendered_at": datetime(2026, 11, 23, 6, 0, tzinfo=UTC),
        }
        data.update(overrides)
        return self._add(ClipRow(candidate_id=candidate.id, **data))

    def approval(self, clip: ClipRow, **overrides: Any) -> ApprovalRow:
        data: dict[str, Any] = {
            "decision": ApprovalDecision.APPROVE,
            "actor": "operator",
            "decided_at": clip.rendered_at + timedelta(minutes=5),
        }
        data.update(overrides)
        return self._add(ApprovalRow(clip_id=clip.id, **data))

    def evening(self, day: date | None = None, **overrides: Any) -> EveningRow:
        return self._add(EveningRow(date=day or date(2026, 11, 23), **overrides))

    def post(self, clip: ClipRow | None = None, platform: Platform = Platform.TIKTOK, **overrides: Any) -> PostRow:
        clip = clip or self.clip()
        data: dict[str, Any] = {"clip_sha256": clip.sha256, "platform": platform}
        data.update(overrides)
        return self._add(PostRow(clip_id=clip.id, **data))

    def submission(self, post: PostRow | None = None, **overrides: Any) -> SubmissionRow:
        post = post or self.post()
        clip = self.session.get(ClipRow, post.clip_id)
        assert clip is not None
        candidate = self.session.get(CandidateRow, clip.candidate_id)
        assert candidate is not None
        source = self.session.get(SourceRow, candidate.source_id)
        assert source is not None
        published = post.published_at or datetime(2026, 11, 24, 1, 10, tzinfo=UTC)
        data: dict[str, Any] = {"campaign_id": source.campaign_id, "deadline_at": published + timedelta(minutes=30)}
        data.update(overrides)
        return self._add(SubmissionRow(post_id=post.id, **data))
