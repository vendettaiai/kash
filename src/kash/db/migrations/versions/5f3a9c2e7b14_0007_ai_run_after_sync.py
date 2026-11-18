"""0007_ai_run_after_sync: categorize new transactions after bank sync (AI phase C)

Revision ID: 5f3a9c2e7b14
Revises: bbd9a0f6c6fe
Create Date: 2026-09-29 21:00:00.000000

Additive: one defaulted column. It only matters once AI is turned on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f3a9c2e7b14"
down_revision: str | Sequence[str] | None = "bbd9a0f6c6fe"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ai_run_after_sync", sa.Boolean(), server_default=sa.true(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("ai_run_after_sync")
