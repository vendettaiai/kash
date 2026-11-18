"""0006_ai_suggestions: AI suggestions, run log, exclusion seeding flag (AI phase B)

Revision ID: bbd9a0f6c6fe
Revises: 0d246c81f49a
Create Date: 2026-09-29 18:09:33.278812

Additive: two new tables and one defaulted column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bbd9a0f6c6fe"
down_revision: str | Sequence[str] | None = "0d246c81f49a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("items_sent", sa.Integer(), nullable=False),
        sa.Column("transactions_covered", sa.Integer(), nullable=False),
        sa.Column("requests", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ai_runs", schema=None) as batch_op:
        batch_op.create_index("idx_ai_runs_user_started", ["user_id", "started_at"], unique=False)

    op.create_table(
        "ai_suggestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(length=160), nullable=True),
        sa.Column("rule_pattern", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", name="uq_ai_suggestion_transaction"),
    )
    with op.batch_alter_table("ai_suggestions", schema=None) as batch_op:
        batch_op.create_index("idx_ai_suggestions_user_status", ["user_id", "status"], unique=False)

    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ai_exclusions_seeded", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.drop_column("ai_exclusions_seeded")

    with op.batch_alter_table("ai_suggestions", schema=None) as batch_op:
        batch_op.drop_index("idx_ai_suggestions_user_status")

    op.drop_table("ai_suggestions")
    with op.batch_alter_table("ai_runs", schema=None) as batch_op:
        batch_op.drop_index("idx_ai_runs_user_started")

    op.drop_table("ai_runs")
