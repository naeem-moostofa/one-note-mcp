"""add sync_jobs queue

Revision ID: a1c2e3f40b5d
Revises: b3f2a9c47e10
Create Date: 2026-06-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.models import (
    SYNC_JOB_ACTIVE_DISCOVERY_WHERE,
    SYNC_JOB_ACTIVE_NOTEBOOK_WHERE,
)


# revision identifiers, used by Alembic.
revision: str = 'a1c2e3f40b5d'
down_revision: Union[str, Sequence[str], None] = 'b3f2a9c47e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2 durable sync-job queue: every sync entry point becomes a producer that
    enqueues here, and one worker drains it as the sole Graph executor. The two partial
    unique indexes make enqueue idempotent (one active job per notebook for content jobs,
    one per connection for discovery jobs)."""
    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("NOTEBOOK_CONTENT", "DISCOVERY", name="sync_job_kind"), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("notebook_id", sa.Integer(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", name="sync_job_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("source", sa.Enum("MANUAL", "AUTO", "CLI", name="sync_job_source"), nullable=False),
        sa.Column("priority", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["microsoft_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["notebook_id"], ["notebooks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_sync_jobs_active_notebook",
        "sync_jobs",
        ["notebook_id", "kind"],
        unique=True,
        postgresql_where=sa.text(SYNC_JOB_ACTIVE_NOTEBOOK_WHERE),
    )
    op.create_index(
        "uq_sync_jobs_active_discovery",
        "sync_jobs",
        ["connection_id", "kind"],
        unique=True,
        postgresql_where=sa.text(SYNC_JOB_ACTIVE_DISCOVERY_WHERE),
    )
    op.create_index(
        "ix_sync_jobs_claim",
        "sync_jobs",
        ["status", "next_run_at", "priority", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sync_jobs_claim", table_name="sync_jobs")
    op.drop_index("uq_sync_jobs_active_discovery", table_name="sync_jobs")
    op.drop_index("uq_sync_jobs_active_notebook", table_name="sync_jobs")
    op.drop_table("sync_jobs")
    sa.Enum(name="sync_job_source").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sync_job_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sync_job_kind").drop(op.get_bind(), checkfirst=True)
