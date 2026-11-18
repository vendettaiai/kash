"""0009_merchant_icons: custom merchant logos uploaded by the user

Revision ID: 2b7d5e9c4a61
Revises: 8c1e4d2a9b37
Create Date: 2026-09-30 15:00:00.000000

Additive: one new table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2b7d5e9c4a61"
down_revision: str | Sequence[str] | None = "8c1e4d2a9b37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_icons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("merchant", sa.String(length=80), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "merchant", name="uq_merchant_icon_merchant"),
    )


def downgrade() -> None:
    op.drop_table("merchant_icons")
