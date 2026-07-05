"""add_oauth_login_flows

Server-side store for in-flight Microsoft logins, keyed by the OAuth `state`
param. Replaces the `oauth_flow` cookie, which the WorkOS bridge's cross-site
redirect bounce caused browsers to drop. See plans/mcp-oauth-web-clients.md.

Revision ID: e5a7c9b1d3f2
Revises: d2e4f6a8c0b1
Create Date: 2026-06-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5a7c9b1d3f2'
down_revision: Union[str, Sequence[str], None] = 'd2e4f6a8c0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'oauth_login_flows',
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('encrypted_flow', sa.Text(), nullable=False),
        sa.Column('external_auth_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('state'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('oauth_login_flows')
