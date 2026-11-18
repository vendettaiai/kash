"""0011_account_cash_flow: per-account income/spending setting; auto-transfer switch

Revision ID: 9a4c7e1f2b58
Revises: 6e2f8a1c3d95
Create Date: 2026-10-01 18:00:00.000000

- ``accounts.cash_flow`` (all | income | none). Investment accounts start at
  "none", which is how they were already treated (left out of income/spending).
- ``user_settings.auto_transfers`` (on), the automatic transfer pairing.
- Sync used to mark every investment-account transaction as a transfer behind
  the scenes. That is gone, so the flag is cleared on those (unpaired, synced)
  transactions; with "none" they don't count either way.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4c7e1f2b58"
down_revision: str | Sequence[str] | None = "6e2f8a1c3d95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cash_flow", sa.String(length=10), server_default="all", nullable=False)
        )
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("auto_transfers", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
    op.execute("UPDATE accounts SET cash_flow = 'none' WHERE account_type = 'investment'")
    op.execute(
        sa.text(
            "UPDATE transactions SET is_transfer = :no "
            "WHERE source = 'sync' AND matched_transfer_id IS NULL AND is_transfer = :yes "
            "AND account_id IN (SELECT id FROM accounts WHERE account_type = 'investment')"
        ).bindparams(no=False, yes=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("auto_transfers")
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("cash_flow")
