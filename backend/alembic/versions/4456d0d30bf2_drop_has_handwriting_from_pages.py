"""drop_has_handwriting_from_pages

Revision ID: 4456d0d30bf2
Revises: 4ce94f7dec27
Create Date: 2026-05-20 01:40:43.171614

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4456d0d30bf2'
down_revision: Union[str, Sequence[str], None] = '4ce94f7dec27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('pages', 'has_handwriting')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('pages', sa.Column('has_handwriting', sa.Boolean(), nullable=False, server_default='false'))
