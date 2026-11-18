"""0008_ai_options: advanced AI settings and dismissed rule proposals (JSON)

Revision ID: 8c1e4d2a9b37
Revises: 5f3a9c2e7b14
Create Date: 2026-09-30 12:00:00.000000

Additive: one nullable JSON column. NULL means "all defaults".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c1e4d2a9b37"
down_revision: str | Sequence[str] | None = "5f3a9c2e7b14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_options",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("ai_options")
