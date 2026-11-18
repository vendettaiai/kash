"""0010_account_show_in_balance: hide an account from the dashboard balance

Revision ID: 6e2f8a1c3d95
Revises: 2b7d5e9c4a61
Create Date: 2026-09-30 18:00:00.000000

Additive: one defaulted column (every account stays in the balance).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6e2f8a1c3d95"
down_revision: str | Sequence[str] | None = "2b7d5e9c4a61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("show_in_balance", sa.Boolean(), server_default=sa.true(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("show_in_balance")
