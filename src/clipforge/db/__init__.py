"""Database layer: models, sessions, state transitions, backup and migrations.

Phase 0 owns this package; Phase 1 branches must not edit it. Need a column? Write a contract request
(docs/contract-requests/<branch>.md).
"""

from clipforge.db.models import (
    ApprovalRow,
    Base,
    CampaignRow,
    CandidateRow,
    ClipRow,
    CostRow,
    EveningRow,
    EventRow,
    FewshotRow,
    FlagRow,
    IllegalTransition,
    PostRow,
    SourceRow,
    SubmissionRow,
    TranscriptRow,
    ViewSnapshotRow,
    VodProposalRow,
    utcnow,
)
from clipforge.db.session import make_engine, make_session_factory, session_scope
from clipforge.db.transitions import record_event, transition

__all__ = [
    "ApprovalRow",
    "Base",
    "CampaignRow",
    "CandidateRow",
    "ClipRow",
    "CostRow",
    "EveningRow",
    "EventRow",
    "FewshotRow",
    "FlagRow",
    "IllegalTransition",
    "PostRow",
    "SourceRow",
    "SubmissionRow",
    "TranscriptRow",
    "ViewSnapshotRow",
    "VodProposalRow",
    "make_engine",
    "make_session_factory",
    "record_event",
    "session_scope",
    "transition",
    "utcnow",
]
