"""drop_content_hash_from_pages

The column was written on every sync but never read for any decision (no change-detection /
re-embedding / dedup ever used it); the page-level last_modified skip already prevents reprocessing
unchanged pages. See plans/attachment-fetch-optimization.md (§6).

Revision ID: d2e4f6a8c0b1
Revises: a1c2e3f40b5d
Create Date: 2026-06-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e4f6a8c0b1'
down_revision: Union[str, Sequence[str], None] = 'a1c2e3f40b5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('pages', 'content_hash')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('pages', sa.Column('content_hash', sa.String(), nullable=True))
