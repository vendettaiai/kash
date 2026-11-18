"""0012_font_scale: text size setting (Settings → Display)

Revision ID: 3e8b5d2a7c41
Revises: 9a4c7e1f2b58
Create Date: 2026-10-01 20:00:00.000000

Additive: one defaulted column (100% = the browser's default size).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e8b5d2a7c41"
down_revision: str | Sequence[str] | None = "9a4c7e1f2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("font_scale", sa.Integer(), server_default="100", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("font_scale")
