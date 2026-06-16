"""notebook last_modified_datetime

Revision ID: b3f2a9c47e10
Revises: 6658bcb39a7f
Create Date: 2026-06-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3f2a9c47e10'
down_revision: Union[str, Sequence[str], None] = '6658bcb39a7f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """OneNote's notebook-level lastModifiedDateTime, captured on every notebook-list
    refresh so the dashboard can sort/filter by when each notebook was last edited.
    Nullable — backfills on the next refresh; existing rows stay NULL until then."""
    op.add_column(
        "notebooks",
        sa.Column("last_modified_datetime", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notebooks", "last_modified_datetime")
