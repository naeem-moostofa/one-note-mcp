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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Enum as SAEnum


class Base(DeclarativeBase):
    pass


class MicrosoftConnectionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NEEDS_REAUTH = "NEEDS_REAUTH"


class NotebookSyncStatus(StrEnum):
    SYNCING = "SYNCING"
    FAILED = "FAILED"
    EXCLUDED = "EXCLUDED"


class PageSyncStatus(StrEnum):
    SYNCING = "SYNCING"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    microsoft_oid = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=False)
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
    display_name = Column(String, nullable=False)
    sync_enabled = Column(Boolean, nullable=False, default=True)
    sync_status = Column(SAEnum(NotebookSyncStatus, name="notebook_sync_status"), nullable=True, default=None)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "onenote_id"),)


class Section(Base):
    __tablename__ = "sections"

    id = Column(Integer, primary_key=True)
    notebook_id = Column(Integer, ForeignKey("notebooks.id", ondelete="CASCADE"), nullable=False)
    onenote_id = Column(String, nullable=False)
    display_name = Column(String, nullable=False)

    __table_args__ = (UniqueConstraint("notebook_id", "onenote_id"),)


class Page(Base):
    __tablename__ = "pages"

    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id", ondelete="CASCADE"), nullable=False)
    onenote_id = Column(String, nullable=False)
    title = Column(String, nullable=True)
    content = Column(Text, nullable=True)
    search_vector = Column(TSVECTOR, Computed("to_tsvector('english', coalesce(content, ''))", persisted=True))
    content_hash = Column(String, nullable=True)
    sync_status = Column(SAEnum(PageSyncStatus, name="page_sync_status"), nullable=True, default=None)

    __table_args__ = (
        UniqueConstraint("section_id", "onenote_id"),
        Index("ix_pages_search_vector_gin", "search_vector", postgresql_using="gin"),
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
