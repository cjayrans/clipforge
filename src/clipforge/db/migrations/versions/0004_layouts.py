"""layouts: campaign layout + manual facecam box, per-source facecam detection, per-clip layout and render time

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25 12:00:00

Only adds columns (campaigns.layout defaults to 'auto'; the rest are nullable): existing campaigns, sources and clips
(approved or published ones included) keep every value. Nothing is re-rendered here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("layout", sa.String(length=16), nullable=False, server_default="auto"))
        batch.add_column(sa.Column("facecam_box", sa.JSON(), nullable=True))
    with op.batch_alter_table("sources") as batch:
        batch.add_column(sa.Column("facecam_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("clips") as batch:
        batch.add_column(sa.Column("layout", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("render_s", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("clips") as batch:
        batch.drop_column("render_s")
        batch.drop_column("layout")
    with op.batch_alter_table("sources") as batch:
        batch.drop_column("facecam_json")
    with op.batch_alter_table("campaigns") as batch:
        batch.drop_column("facecam_box")
        batch.drop_column("layout")
