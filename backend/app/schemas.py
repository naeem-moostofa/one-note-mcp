from datetime import datetime
from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.models import MicrosoftConnectionStatus, NotebookSyncStatus, PageSyncStatus

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    data: list[T]
    total: int
    limit: int
    offset: int
# --- Response schemas ---

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    microsoft_oid: str
    email: str
    display_name: str
    created_at: datetime


class MicrosoftConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    encrypted_msal_token_cache: str
    status: MicrosoftConnectionStatus


class NotebookResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    onenote_id: str
    display_name: str
    sync_enabled: bool
    sync_status: Optional[NotebookSyncStatus] = None
    last_synced_at: Optional[datetime] = None


class SectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    notebook_id: int
    onenote_id: str
    display_name: str


class PageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    section_id: int
    onenote_id: str
    title: Optional[str] = None
    content: Optional[str] = None
    content_hash: Optional[str] = None
    sync_status: Optional[PageSyncStatus] = None


class PageDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    onenote_id: str
    title: Optional[str] = None
    content: Optional[str] = None
    content_hash: Optional[str] = None
    sync_status: Optional[PageSyncStatus] = None
    section_name: str
    notebook_name: str


class PageSearchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    onenote_id: str
    title: Optional[str] = None
    content_excerpt: Optional[str] = None
    section_name: str
    notebook_name: str
    sync_status: Optional[PageSyncStatus] = None


class MCPConnectionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    token_hash: str
    display_name: Optional[str] = None
    scope_all_notebooks: bool
    notebook_ids: Optional[list[int]] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


# --- Create schemas ---

class UserCreate(BaseModel):
    microsoft_oid: str
    email: str
    display_name: str


class MicrosoftConnectionCreate(BaseModel):
    encrypted_msal_token_cache: str


class NotebookCreate(BaseModel):
    onenote_id: str
    display_name: str


class SectionCreate(BaseModel):
    onenote_id: str
    display_name: str


class PageCreate(BaseModel):
    onenote_id: str
    title: Optional[str] = None


class MCPConnectionCreate(BaseModel):
    token_hash: str
    display_name: Optional[str] = None
    scope_all_notebooks: bool
    notebook_ids: Optional[list[int]] = None


# --- Update schemas (all fields optional, use exclude_unset=True with model_dump) ---

class MicrosoftConnectionUpdate(BaseModel):
    encrypted_msal_token_cache: Optional[str] = None
    status: Optional[MicrosoftConnectionStatus] = None


class NotebookUpdate(BaseModel):
    sync_enabled: Optional[bool] = None
    sync_status: Optional[NotebookSyncStatus] = None
    last_synced_at: Optional[datetime] = None


class PageUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    content_hash: Optional[str] = None
    sync_status: Optional[PageSyncStatus] = None


class MCPConnectionUpdate(BaseModel):
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class PageSearchQuery(BaseModel):
    query: str
    limit: int
    offset: int = 0
    notebook_ids: list[int]


# --- Search service schemas ---

class PageFTSHit(BaseModel):
    """Single FTS match returned by PageRepository.search_fts."""
    page_id: int
    rank: float
    content: str


class PageTrgmHit(BaseModel):
    """Single trigram fuzzy match returned by PageRepository.search_trgm."""
    page_id: int
    score: float  # max word_similarity across the matched terms
    content: str


class PageWithPath(BaseModel):
    """Path + staleness metadata used to build SearchHit responses."""
    model_config = ConfigDict(from_attributes=True)

    page_id: int
    page_title: Optional[str] = None
    section_name: str
    notebook_id: int
    notebook_name: str
    page_sync_status: Optional[PageSyncStatus] = None
    notebook_sync_status: Optional[NotebookSyncStatus] = None


class SearchSnippet(BaseModel):
    """A character window of `pages.content` around one or more match offsets."""
    text: str
    start_offset: int  # offset into pages.content where this window starts


class SearchHit(BaseModel):
    """One page in the SearchService.search result list."""
    page_id: int
    page_title: Optional[str] = None
    section_name: str
    notebook_id: int
    notebook_name: str
    snippets: list[SearchSnippet]
    stale: bool


# --- Client schemas ---

class MSALIDTokenClaims(BaseModel):
    model_config = ConfigDict(extra="ignore")

    oid: str
    preferred_username: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None


class MSALAuthCodeFlow(BaseModel):
    model_config = ConfigDict(extra="allow")

    auth_uri: str
    state: str


class MSALTokenResult(BaseModel):
    access_token: str
    id_token_claims: MSALIDTokenClaims
    serialized_cache: str


class MSALSilentTokenResult(BaseModel):
    access_token: str
    serialized_cache: str


class GraphNotebook(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    display_name: str = Field(alias="displayName")
    last_modified_datetime: datetime = Field(alias="lastModifiedDateTime")


class GraphSection(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    display_name: str = Field(alias="displayName")


class GraphPage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: Optional[str] = None
    last_modified_datetime: datetime = Field(alias="lastModifiedDateTime")


class GraphPageElement(BaseModel):
    kind: Literal["text", "image"]
    text: str | None = None
    image_url: str | None = None
    # CSS absolute position — only meaningful for kind="image", used for composite rendering
    top: float = 0.0
    left: float = 0.0
    width: float = 0.0
    height: float = 0.0


class GraphPageContent(BaseModel):
    elements: list[GraphPageElement]  # ordered by CSS top/left — visual reading order
    ink_strokes: list[list[tuple[float, float]]]  # HiMetric coords; empty list if no ink
    has_handwriting: bool
