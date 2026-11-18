"""0016_household_logins: several people log in to one household

Revision ID: 7d2a4f8c1e56
Revises: 6c9e3a1f7d45
Create Date: 2026-10-05 12:00:00.000000

Additive: per-person theme and text size (empty = the household setting), and a
one-time invite / password link (stored as a SHA-256 hash with an expiry).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d2a4f8c1e56"
down_revision: str | Sequence[str] | None = "6c9e3a1f7d45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("theme", sa.String(length=10), nullable=True))
        batch_op.add_column(sa.Column("font_scale", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("invite_token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_users_invite_token_hash"), ["invite_token_hash"], unique=True
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_invite_token_hash"))
        batch_op.drop_column("invite_expires_at")
        batch_op.drop_column("invite_token_hash")
        batch_op.drop_column("font_scale")
        batch_op.drop_column("theme")
