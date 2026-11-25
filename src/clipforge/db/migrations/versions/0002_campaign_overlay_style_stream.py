"""campaign overlay, copy style, stream channel/length limits; candidate hook history

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24 20:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("overlay", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("copy_style_notes", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("stream_channels", sa.JSON(), nullable=False, server_default="[]"))
        batch.add_column(sa.Column("max_source_minutes", sa.Integer(), nullable=True))
    with op.batch_alter_table("candidates") as batch:
        batch.add_column(sa.Column("hook_history", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    with op.batch_alter_table("candidates") as batch:
        batch.drop_column("hook_history")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("max_source_minutes")
        batch.drop_column("stream_channels")
        batch.drop_column("copy_style_notes")
        batch.drop_column("overlay")
