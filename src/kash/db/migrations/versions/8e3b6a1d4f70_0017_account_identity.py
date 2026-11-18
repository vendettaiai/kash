"""0017_account_identity: recognize bank accounts under a new provider id

Revision ID: 8e3b6a1d4f70
Revises: 7d2a4f8c1e56
Create Date: 2026-10-08 12:00:00.000000

Additive only: the provider's own account name and the bank's id on each account
(so a bank removed and re-added in SimpleFIN is matched to the accounts Kash already
has); on each connection, new accounts waiting for the user to say whether they are
new or replace one (e.g. a replaced card), and provider accounts to ignore (a
replaced card the bank still reports).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8e3b6a1d4f70"
down_revision: str | Sequence[str] | None = "7d2a4f8c1e56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider_account_name", sa.String(length=255), nullable=True)
        )
        batch_op.add_column(sa.Column("institution_id", sa.String(length=255), nullable=True))
    with op.batch_alter_table("bank_connections", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "pending_accounts",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "retired_account_ids",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("bank_connections", schema=None) as batch_op:
        batch_op.drop_column("retired_account_ids")
        batch_op.drop_column("pending_accounts")
    with op.batch_alter_table("accounts", schema=None) as batch_op:
        batch_op.drop_column("institution_id")
        batch_op.drop_column("provider_account_name")
