"""0018_sync_cron: custom (cron) schedule for automatic bank sync

Revision ID: 9f4c7b2e5a81
Revises: 8e3b6a1d4f70
Create Date: 2026-10-08 12:30:00.000000

Additive: two nullable columns. NULL mode means the existing interval schedule, so
saved settings keep working exactly as before.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f4c7b2e5a81"
down_revision: str | Sequence[str] | None = "8e3b6a1d4f70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sync_mode", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("sync_cron", sa.String(length=120), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("sync_cron")
        batch_op.drop_column("sync_mode")
