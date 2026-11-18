"""0013_drop_ai_exclusions: the AI prompt decides what to leave alone

Revision ID: 4a7c1e9d5b23
Revises: 3e8b5d2a7c41
Create Date: 2026-10-01 21:00:00.000000

Removes the seeded "ai_exclusion" rules (they only kept Zelle, Venmo, … away from
the AI) and the flag that remembered they were seeded.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4a7c1e9d5b23"
down_revision: str | Sequence[str] | None = "3e8b5d2a7c41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DELETE FROM rules WHERE rule_type = 'ai_exclusion'")
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("ai_exclusions_seeded")


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_exclusions_seeded", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )
