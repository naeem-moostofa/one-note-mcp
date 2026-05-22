"""add_has_handwriting_to_pages

Revision ID: 4ce94f7dec27
Revises: c51d7c14c289
Create Date: 2026-05-20 01:37:50.432816

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ce94f7dec27'
down_revision: Union[str, Sequence[str], None] = 'c51d7c14c289'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('pages', sa.Column('has_handwriting', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('pages', 'has_handwriting')
