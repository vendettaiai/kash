"""0015_drop_auto_transfers: transfer pairing and card/loan payments are always on

Revision ID: 6c9e3a1f7d45
Revises: 5b8d2f0e6c34
Create Date: 2026-10-02 15:00:00.000000

Removes the "Pair transfers between my accounts automatically" switch. Pairing is
controlled by the transfer matching rules; payments into a credit card or loan account
are always treated as transfers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6c9e3a1f7d45"
down_revision: str | Sequence[str] | None = "5b8d2f0e6c34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("auto_transfers")


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_transfers", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
