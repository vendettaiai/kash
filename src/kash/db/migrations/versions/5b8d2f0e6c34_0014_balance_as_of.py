"""0014_balance_as_of: when the bank last reported each account's balance

Revision ID: 5b8d2f0e6c34
Revises: 4a7c1e9d5b23
Create Date: 2026-10-02 12:00:00.000000

Additive: one nullable column (SimpleFIN's balance-date).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8d2f0e6c34"
down_revision: str | Sequence[str] | None = "4a7c1e9d5b23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("balance_as_of", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("balance_as_of")
