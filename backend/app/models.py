import re
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Enum as SAEnum


class Base(DeclarativeBase):
    pass


# NUL is rejected by Postgres text; lone surrogates have no UTF-8 encoding (asyncpg can't send
# them). These are the only two characters that break a write.
_POSTGRES_UNSAFE_TEXT = re.compile(r"[\x00\ud800-\udfff]")


class SanitizedText(TypeDecorator):
    """Text column that strips characters Postgres+asyncpg can't store, on write.

    For columns populated from external Graph/OCR/PDF text."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return None if value is None else _POSTGRES_UNSAFE_TEXT.sub("", value)


class MicrosoftConnectionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NEEDS_REAUTH = "NEEDS_REAUTH"


class NotebookSyncStatus(StrEnum):
    PENDING = "PENDING"
    SYNCING = "SYNCING"
    FRESH = "FRESH"
    FAILED = "FAILED"


class PageSyncStatus(StrEnum):
    PENDING = "PENDING"
    SYNCING = "SYNCING"
    FRESH = "FRESH"
    FAILED = "FAILED"


class SyncJobKind(StrEnum):
    NOTEBOOK_CONTENT = "NOTEBOOK_CONTENT"  # sync one notebook's sections + pages
    DISCOVERY = "DISCOVERY"                # names-only list refresh + fan-out of content jobs


class SyncJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SyncJobSource(StrEnum):
    MANUAL = "MANUAL"  # user clicked Sync in the UI
    AUTO = "AUTO"      # cron-driven discovery / fan-out
    CLI = "CLI"        # python -m sync.run --notebook-id


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    microsoft_oid = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(SanitizedText, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MicrosoftConnection(Base):
    __tablename__ = "microsoft_connections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    encrypted_msal_token_cache = Column(Text, nullable=False)
    status = Column(SAEnum(MicrosoftConnectionStatus, name="ms_conn_status"), nullable=False, default=MicrosoftConnectionStatus.ACTIVE)


class Notebook(Base):
    __tablename__ = "notebooks"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    onenote_id = Column(String, nullable=False)
    display_name = Column(SanitizedText, nullable=False)
    sync_enabled = Column(Boolean, nullable=False, server_default=text("false"), default=False)
    sync_status = Column(
        SAEnum(NotebookSyncStatus, name="notebook_sync_status"),
        nullable=False,
        server_default=NotebookSyncStatus.PENDING.value,
    )
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    last_modified_datetime = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "onenote_id"),)


class Section(Base):
    __tablename__ = "sections"

    id = Column(Integer, primary_key=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False)
    onenote_id = Column(String, nullable=False)
    display_name = Column(SanitizedText, nullable=False)

    __table_args__ = (UniqueConstraint("notebook_id", "onenote_id"),)


class Page(Base):
    __tablename__ = "pages"

    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id", ondelete="CASCADE"), nullable=False)
    onenote_id = Column(String, nullable=False)
    title = Column(SanitizedText, nullable=True)
    content = Column(SanitizedText, nullable=True)
    search_vector = Column(TSVECTOR, Computed("to_tsvector('english', coalesce(content, ''))", persisted=True))
    sync_status = Column(
        SAEnum(PageSyncStatus, name="page_sync_status"),
        nullable=False,
        server_default=PageSyncStatus.PENDING.value,
    )

    __table_args__ = (
        UniqueConstraint("section_id", "onenote_id"),
        Index("ix_pages_search_vector_gin", "search_vector", postgresql_using="gin"),
        Index(
            "ix_pages_content_trgm",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": "gin_trgm_ops"},
        ),
    )


# Partial-index predicates shared by the model (below), the migration, and the repository's
# ON CONFLICT inference. Kept textually identical so Postgres matches the arbiter index exactly.
SYNC_JOB_ACTIVE_NOTEBOOK_WHERE = "status IN ('PENDING', 'RUNNING') AND notebook_id IS NOT NULL"
SYNC_JOB_ACTIVE_DISCOVERY_WHERE = "status IN ('PENDING', 'RUNNING') AND notebook_id IS NULL"


class SyncJob(Base):
    """Durable unit of sync work drained by the single worker process (Phase 2).

    Every sync entry point (UI, cron, CLI) is a producer that enqueues a row here; exactly one
    worker claims and runs them, so the in-process Graph rate limiter governs one executor. The
    two partial unique indexes enforce "at most one active job" per notebook (for content jobs)
    and per connection (for discovery jobs), making enqueue idempotent under spam-clicks and
    overlapping cron runs. See plans/sync-rate-limit-fix-plan.md."""

    __tablename__ = "sync_jobs"

    id = Column(Integer, primary_key=True)
    kind = Column(SAEnum(SyncJobKind, name="sync_job_kind"), nullable=False)
    connection_id = Column(Integer, ForeignKey("microsoft_connections.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # null for discovery jobs; cascades so deleting a notebook clears its queued/running jobs.
    notebook_id = Column(Integer, ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=True)
    status = Column(
        SAEnum(SyncJobStatus, name="sync_job_status"),
        nullable=False,
        server_default=SyncJobStatus.PENDING.value,
    )
    source = Column(SAEnum(SyncJobSource, name="sync_job_source"), nullable=False)
    priority = Column(Integer, nullable=False, server_default=text("0"))  # higher first; manual > auto
    attempts = Column(Integer, nullable=False, server_default=text("0"))
    max_attempts = Column(Integer, nullable=False, server_default=text("5"))
    next_run_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_sync_jobs_active_notebook",
            "notebook_id",
            "kind",
            unique=True,
            postgresql_where=text(SYNC_JOB_ACTIVE_NOTEBOOK_WHERE),
        ),
        Index(
            "uq_sync_jobs_active_discovery",
            "connection_id",
            "kind",
            unique=True,
            postgresql_where=text(SYNC_JOB_ACTIVE_DISCOVERY_WHERE),
        ),
        # Claim path: WHERE status='PENDING' AND next_run_at <= now() ORDER BY priority DESC, created_at.
        Index("ix_sync_jobs_claim", "status", "next_run_at", "priority", "created_at"),
    )


class MCPConnection(Base):
    __tablename__ = "mcp_connections"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=True)
    scope_all_notebooks = Column(Boolean, nullable=False, default=True)
    notebook_ids = Column(ARRAY(Integer), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
