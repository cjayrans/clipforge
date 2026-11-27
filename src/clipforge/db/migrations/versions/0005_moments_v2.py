"""moments-v2: six-part rubric and finder per candidate, viewer-response metrics per post, per-source cost tracking,
VOD proposals, campaign VOD-proposal settings

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26 12:00:00

Only adds columns and one table (all nullable, or with a default): existing campaigns, sources, candidates, clips and
posts (approved or published ones included) keep every value. The four v1 moment scores stay for history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import clipforge.db.models

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUBRIC = ("hook", "arc", "arousal", "sendability", "clarity", "density")


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("auto_propose_vods", sa.Boolean(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("recent_vod_days", sa.Integer(), nullable=True))
    with op.batch_alter_table("candidates") as batch:
        for name in RUBRIC:
            batch.add_column(sa.Column(f"score_{name}", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("scores_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("found_by", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("content_confidence", sa.Float(), nullable=True))
        batch.add_column(sa.Column("vision_json", sa.JSON(), nullable=True))
        batch.create_index(batch.f("ix_candidates_found_by"), ["found_by"], unique=False)
    with op.batch_alter_table("posts") as batch:
        batch.add_column(sa.Column("avg_watch_time_s", sa.Float(), nullable=True))
        batch.add_column(sa.Column("completion_rate", sa.Float(), nullable=True))
        batch.add_column(sa.Column("shares", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("likes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("metrics_at", clipforge.db.models.UTCDateTime(), nullable=True))
    with op.batch_alter_table("costs") as batch:
        batch.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("stage", sa.String(length=32), nullable=True))
        batch.create_index(batch.f("ix_costs_source_id"), ["source_id"], unique=False)
    op.create_table(
        "vod_proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("video_id", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("vod_created_at", clipforge.db.models.UTCDateTime(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("proposed_at", clipforge.db.models.UTCDateTime(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("decided_at", clipforge.db.models.UTCDateTime(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], name=op.f("fk_vod_proposals_campaign_id_campaigns")),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_vod_proposals_source_id_sources")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vod_proposals")),
        sa.UniqueConstraint("campaign_id", "url", name=op.f("uq_vod_proposals_campaign_id_url")),
    )
    with op.batch_alter_table("vod_proposals") as batch:
        batch.create_index(batch.f("ix_vod_proposals_campaign_id"), ["campaign_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("vod_proposals") as batch:
        batch.drop_index(batch.f("ix_vod_proposals_campaign_id"))
    op.drop_table("vod_proposals")
    with op.batch_alter_table("costs") as batch:
        batch.drop_index(batch.f("ix_costs_source_id"))
        batch.drop_column("stage")
        batch.drop_column("source_id")
    with op.batch_alter_table("posts") as batch:
        for name in ("metrics_at", "likes", "shares", "completion_rate", "avg_watch_time_s"):
            batch.drop_column(name)
    with op.batch_alter_table("candidates") as batch:
        batch.drop_index(batch.f("ix_candidates_found_by"))
        for name in ("vision_json", "content_confidence", "found_by", "scores_json"):
            batch.drop_column(name)
        for name in reversed(RUBRIC):
            batch.drop_column(f"score_{name}")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("recent_vod_days")
        batch.drop_column("auto_propose_vods")
