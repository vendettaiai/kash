"""0019_fill_empty_descriptions: name transactions the bank sent without a description

Revision ID: a1f6d3c8e245
Revises: 9f4c7b2e5a81
Create Date: 2026-11-01 08:30:00.000000

Data only: from mid-October 2026 SimpleFIN sent an empty description for some accounts,
so those rows have no name and rules can't match them. They take their payee, or the
same wording sync now uses when there is none. Rows with any text are left alone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f6d3c8e245"
down_revision: str | Sequence[str] | None = "9f4c7b2e5a81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYEE_PLACEHOLDER = "(Enter transaction description here.)"
NO_DESCRIPTION = "No description from bank"
EMPTY = "TRIM(clean_description) = '' AND TRIM(original_description) = ''"


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE transactions SET merchant_name = NULL WHERE merchant_name = :placeholder"
        ).bindparams(placeholder=PAYEE_PLACEHOLDER)
    )
    op.execute(
        "UPDATE transactions SET original_description = merchant_name, "
        f"clean_description = merchant_name WHERE {EMPTY} "
        "AND merchant_name IS NOT NULL AND TRIM(merchant_name) <> ''"
    )
    op.execute(
        sa.text(
            "UPDATE transactions SET original_description = :text, clean_description = :text "
            f"WHERE {EMPTY}"
        ).bindparams(text=NO_DESCRIPTION)
    )


def downgrade() -> None:
    # The empty descriptions carried no information; nothing to restore
    pass
