"""notebook sync_enabled default false

Revision ID: 6658bcb39a7f
Revises: cd67f79f85ca
Create Date: 2026-06-15 03:38:19.918562

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6658bcb39a7f'
down_revision: Union[str, Sequence[str], None] = 'cd67f79f85ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Newly-discovered notebooks default to NOT auto-syncing — the user opts each
    one in. Only changes the column default; existing rows keep their value."""
    op.alter_column("notebooks", "sync_enabled", server_default=sa.text("false"))


def downgrade() -> None:
    op.alter_column("notebooks", "sync_enabled", server_default=None)
