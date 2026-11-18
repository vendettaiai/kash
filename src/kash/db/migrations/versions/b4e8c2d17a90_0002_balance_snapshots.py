"""0002_balance_snapshots: daily account balances for net worth history

Revision ID: b4e8c2d17a90
Revises: c36adc0a21cb
Create Date: 2026-09-29 12:00:00.000000

A new table only; databases created by ``create_all`` get it at startup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4e8c2d17a90"
down_revision: str | Sequence[str] | None = "c36adc0a21cb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("balance", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "date", name="uq_snapshot_account_date"),
    )
    with op.batch_alter_table("balance_snapshots", schema=None) as batch_op:
        batch_op.create_index("idx_snapshots_date", ["date"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("balance_snapshots", schema=None) as batch_op:
        batch_op.drop_index("idx_snapshots_date")
    op.drop_table("balance_snapshots")
