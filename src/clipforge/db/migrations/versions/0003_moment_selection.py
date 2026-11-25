"""moment selection: campaign moment profile, per-candidate archetype/scores/gates, post 24 h / 72 h views

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24 23:30:00

Only adds nullable columns: existing campaigns, candidates, clips and posts (including approved ones) are left
exactly as they are; nothing is re-ranked or backfilled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("moment_profile", sa.String(length=64), nullable=True))
    with op.batch_alter_table("candidates") as batch:
        batch.add_column(sa.Column("moment_profile", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("weights_version", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("archetype", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("score_reaction", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("score_event", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("score_setup", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("score_absurdity", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("gate_results_json", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("skip_flags", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("review_flags", sa.JSON(), nullable=True))
        batch.create_index(batch.f("ix_candidates_archetype"), ["archetype"], unique=False)
    with op.batch_alter_table("posts") as batch:
        batch.add_column(sa.Column("views_24h", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("views_72h", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("posts") as batch:
        batch.drop_column("views_72h")
        batch.drop_column("views_24h")
    with op.batch_alter_table("candidates") as batch:
        batch.drop_index(batch.f("ix_candidates_archetype"))
        for name in (
            "review_flags",
            "skip_flags",
            "gate_results_json",
            "score_absurdity",
            "score_setup",
            "score_event",
            "score_reaction",
            "archetype",
            "weights_version",
            "moment_profile",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("moment_profile")
