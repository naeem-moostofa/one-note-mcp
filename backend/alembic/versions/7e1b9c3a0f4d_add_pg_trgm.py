"""add_pg_trgm

Revision ID: 7e1b9c3a0f4d
Revises: 4456d0d30bf2
Create Date: 2026-05-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7e1b9c3a0f4d'
down_revision: Union[str, Sequence[str], None] = '4456d0d30bf2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX ix_pages_content_trgm "
        "ON pages USING gin (content gin_trgm_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_pages_content_trgm")
    # leave pg_trgm in place — may be relied on by other indexes
