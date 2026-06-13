"""cleanup sync status model

Revision ID: cd67f79f85ca
Revises: 7e1b9c3a0f4d
Create Date: 2026-06-08 04:16:55.555937

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd67f79f85ca'
down_revision: Union[str, Sequence[str], None] = '7e1b9c3a0f4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Both sync_status enums → PENDING/SYNCING/FRESH/FAILED, non-nullable, default PENDING.
    Drops EXCLUDED and the nullable NULL-means-fresh overload. Existing rows: NULL/EXCLUDED → PENDING.
    """
    for table, enum_name in (("notebooks", "notebook_sync_status"), ("pages", "page_sync_status")):
        # Detach the column from the enum so the old type can be dropped.
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sync_status TYPE text USING sync_status::text")
        op.execute(f"DROP TYPE {enum_name}")
        # Collapse old/absent values onto the new explicit baseline.
        op.execute(
            f"UPDATE {table} SET sync_status = 'PENDING' "
            f"WHERE sync_status IS NULL OR sync_status NOT IN ('SYNCING', 'FAILED')"
        )
        op.execute(f"CREATE TYPE {enum_name} AS ENUM ('PENDING', 'SYNCING', 'FRESH', 'FAILED')")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN sync_status TYPE {enum_name} USING sync_status::{enum_name}"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sync_status SET DEFAULT 'PENDING'")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sync_status SET NOT NULL")


def downgrade() -> None:
    """Best-effort reverse: back to the old nullable enums (PENDING/FRESH collapse to NULL)."""
    for table, enum_name, old_values in (
        ("notebooks", "notebook_sync_status", "'SYNCING', 'FAILED', 'EXCLUDED'"),
        ("pages", "page_sync_status", "'SYNCING', 'FAILED'"),
    ):
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sync_status DROP DEFAULT")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sync_status DROP NOT NULL")
        op.execute(f"ALTER TABLE {table} ALTER COLUMN sync_status TYPE text USING sync_status::text")
        op.execute(f"DROP TYPE {enum_name}")
        # PENDING/FRESH had no equivalent in the old model — NULL was "fresh / never synced".
        op.execute(f"UPDATE {table} SET sync_status = NULL WHERE sync_status NOT IN ('SYNCING', 'FAILED')")
        op.execute(f"CREATE TYPE {enum_name} AS ENUM ({old_values})")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN sync_status TYPE {enum_name} USING sync_status::{enum_name}"
        )
