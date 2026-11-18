"""0005_ai_settings: optional AI categorization settings (docs/AI_PLAN.md, phase A)

Revision ID: 0d246c81f49a
Revises: ddaa93253fbc
Create Date: 2026-09-29 18:00:00.000000

Additive. AI stays off (ai_enabled = false) until the user turns it on.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0d246c81f49a"
down_revision: str | Sequence[str] | None = "ddaa93253fbc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = (
    "ai_monthly_limit",
    "ai_auto_apply_threshold",
    "ai_api_key_encrypted",
    "ai_model",
    "ai_base_url",
    "ai_provider",
    "ai_enabled",
)


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ai_enabled", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(sa.Column("ai_provider", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("ai_base_url", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("ai_model", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("ai_api_key_encrypted", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("ai_auto_apply_threshold", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("ai_monthly_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        for name in COLUMNS:
            batch_op.drop_column(name)
