"""0004_user_display_name: the owner's name, entered when creating the account

Revision ID: ddaa93253fbc
Revises: 1d9b2df850e7
Create Date: 2026-09-29 13:45:00.000000

Replaces the OWNER_NAME environment variable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ddaa93253fbc"
down_revision: str | Sequence[str] | None = "1d9b2df850e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("display_name")
