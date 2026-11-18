"""0003_sync_settings: automatic bank sync settings in the app

Revision ID: 1d9b2df850e7
Revises: b4e8c2d17a90
Create Date: 2026-09-29 13:21:23.427683

Nullable columns: NULL means "use SYNC_SCHEDULE_HOURS / SYNC_TIMEZONE".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1d9b2df850e7"
down_revision: str | Sequence[str] | None = "b4e8c2d17a90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sync_enabled", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("sync_interval_hours", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("sync_timezone", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("sync_timezone")
        batch_op.drop_column("sync_interval_hours")
        batch_op.drop_column("sync_enabled")
