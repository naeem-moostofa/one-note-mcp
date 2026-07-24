from datetime import datetime
from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.models import (
    MicrosoftConnectionStatus,
    NotebookSyncStatus,
    PageSyncStatus,
    SyncJobKind,
    SyncJobSource,
    SyncJobStatus,
)

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


class OAuthLoginFlowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    state: str
    encrypted_flow: str
    external_auth_id: str | None
    created_at: datetime


class NotebookResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    onenote_id: str
    display_name: str
    sync_enabled: bool
    sync_status: NotebookSyncStatus
    last_synced_at: Optional[datetime] = None
    last_modified_datetime: Optional[datetime] = None


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
    sync_status: PageSyncStatus


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


class MCPConnectionCreatedResponse(BaseModel):
    """Returned by `MCPConnectionService.create` — the only place `raw_token`
    is ever exposed. Subsequent reads of the connection won't include it (the
    DB only stores the hash). Callers are expected to surface the raw token
    to the user once, then discard.

    Internal fields (`user_id`, `token_hash`, `last_used_at`, `revoked_at`)
    are intentionally omitted — at creation time they're either implicit (the
    caller is the owning user) or trivially known (timestamps are null,
    token_hash is internal-only)."""
    id: int
    display_name: Optional[str] = None
    scope_all_notebooks: bool
    notebook_ids: Optional[list[int]] = None
    created_at: datetime
    raw_token: str
    mcp_url: str  # the MCP endpoint the client connects to, e.g. http://localhost:8000/mcp


# --- Web-facing schemas (sanitized; consumed by the React frontend) ---

class MeResponse(BaseModel):
    """Account page payload: profile + Microsoft connection status (never the MSAL cache)."""
    id: int
    email: str
    display_name: str
    created_at: datetime
    microsoft_status: Optional[MicrosoftConnectionStatus] = None  # None = not connected


class NotebookWebResponse(BaseModel):
    """Notebook as shown on the web Notebooks page. sync_status is orthogonal to sync_enabled."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str
    sync_enabled: bool
    sync_status: NotebookSyncStatus
    last_synced_at: Optional[datetime] = None
    last_modified_datetime: Optional[datetime] = None


class NotebookFilter(BaseModel):
    """Query filters + pagination for GET /api/notebooks."""
    search: Optional[str] = Field(default=None, max_length=100)
    sync_enabled: Optional[bool] = None
    sync_status: Optional[NotebookSyncStatus] = None
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class MCPConnectionWebResponse(BaseModel):
    """MCP connection as listed on the web — no token material (the router's
    response_model strips token_hash/user_id from the internal shape)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: Optional[str] = None
    scope_all_notebooks: bool
    notebook_ids: Optional[list[int]] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class NotebookSyncToggleRequest(BaseModel):
    """PATCH /api/notebooks/{id} body — the client may only flip sync_enabled."""
    sync_enabled: bool


class MCPConnectionCreateRequest(BaseModel):
    """POST /api/mcp-connections body. Scope/ownership validation lives in the service."""
    display_name: Optional[str] = None
    scope_all_notebooks: bool
    notebook_ids: Optional[list[int]] = None


# --- Create schemas ---

class UserCreate(BaseModel):
    microsoft_oid: str
    email: str
    display_name: str


class MicrosoftConnectionCreate(BaseModel):
    encrypted_msal_token_cache: str


class OAuthLoginFlowCreate(BaseModel):
    state: str
    encrypted_flow: str
    external_auth_id: str | None = None


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
    last_modified_datetime: Optional[datetime] = None


class PageUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
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
    """A page id with its full-text-search rank."""
    page_id: int
    rank: float


class PageTrgmHit(BaseModel):
    """A page id with its trigram similarity score."""
    page_id: int
    score: float  # max word_similarity across the matched terms


class PageWithPath(BaseModel):
    """A page's content and notebook/section path, with the sync status of both."""
    model_config = ConfigDict(from_attributes=True)

    page_id: int
    page_title: Optional[str] = None
    content: Optional[str] = None
    section_name: str
    notebook_id: int
    notebook_name: str
    page_sync_status: Optional[PageSyncStatus] = None
    notebook_sync_status: Optional[NotebookSyncStatus] = None


class SearchSnippet(BaseModel):
    """A passage of page text, with the coordinates it was cut from.

    `snippet_id` encodes those coordinates, so nothing is stored server-side.
    `text` is None when `error` is set.
    """
    snippet_id: str
    page: str  # breadcrumb: "CS241(1) > Week 5 > Module 5 Part 1"
    text: Optional[str] = None
    error: Optional[str] = None
    stale: bool = False

    @model_serializer
    def _serialize(self) -> dict[str, object]:
        """Emit only the fields that carry information.

        Repeating `"error":null,"stale":false` on every snippet is pure overhead.
        `stale` still ships when true — silently presenting a mid-sync page's
        partial content as complete would be a wrong answer.
        """
        data: dict[str, object] = {"snippet_id": self.snippet_id, "page": self.page}
        if self.text is not None:
            data["text"] = self.text
        if self.error is not None:
            data["error"] = self.error
        if self.stale:
            data["stale"] = True
        return data


class PageDetailResponse(BaseModel):
    """Single-page detail with full content and surrounding section/notebook context.

    `notebook_last_synced_at` comes from the notebook because pages don't carry
    their own last-synced timestamp — they're synced as part of a notebook run.
    """
    model_config = ConfigDict(from_attributes=True)

    page_id: int
    onenote_id: str
    page_title: Optional[str] = None
    content: Optional[str] = None
    page_sync_status: Optional[PageSyncStatus] = None
    section_name: str
    notebook_id: int
    notebook_name: str
    notebook_sync_status: Optional[NotebookSyncStatus] = None
    notebook_last_synced_at: Optional[datetime] = None


# --- MCP-layer schemas (what tools return to the calling LLM) ---


class ResolvedMCPConnection(BaseModel):
    """An authenticated MCP connection with its allowed notebook scope already resolved.

    `allowed_notebook_ids` is the intersection of the connection's stored scope
    (either all notebooks for the user, or a specific list) with the user's
    currently sync-enabled notebooks. Empty list = nothing visible.
    """
    connection_id: int
    user_id: int
    allowed_notebook_ids: list[int]


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


class GraphList(BaseModel, Generic[T]):
    """A Graph collection and whether the enumeration was provably complete.

    When `complete` is False (couldn't confirm via `@odata.count`), callers must not treat a
    missing entry as a deletion — a partial response would wipe live rows."""

    items: list[T] = Field(default_factory=list)
    complete: bool = True


class GraphPageElement(BaseModel):
    kind: Literal["text", "image", "pdf_attachment"]
    text: str | None = None
    image_url: str | None = None
    # CSS absolute position — only meaningful for kind="image", used for composite rendering
    top: float = 0.0
    left: float = 0.0
    width: float = 0.0
    height: float = 0.0
    # kind="pdf_attachment" only: a OneNote "file printout" keeps the source PDF as one resource.
    attachment_name: str | None = None
    resource_url: str | None = None


class GraphPageContent(BaseModel):
    elements: list[GraphPageElement]  # ordered by CSS top/left — visual reading order
    ink_strokes: list[list[tuple[float, float]]]  # HiMetric coords; empty list if no ink
    has_handwriting: bool


# --- Sync service schemas ---


class SectionPages(BaseModel):
    section: SectionResponse
    graph_pages: list[GraphPage]
    pages_complete: bool = True  # False → skip delete-stale (a partial list mustn't wipe pages)


class PageContentSyncCandidate(BaseModel):
    section_name: str
    page: PageResponse


class SectionSyncPlan(BaseModel):
    latest_page_modified: datetime | None = None
    pages_to_sync: list[PageContentSyncCandidate] = Field(default_factory=list)


class PageContentSyncResult(BaseModel):
    page_id: int
    title: str | None = None
    onenote_id: str
    content: str | None = None
    sync_status: PageSyncStatus = PageSyncStatus.FRESH
    error_message: str | None = None


# --- Sync-job queue schemas (Phase 2) ---


class SyncJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: SyncJobKind
    connection_id: int
    user_id: int
    notebook_id: Optional[int] = None
    status: SyncJobStatus
    source: SyncJobSource
    priority: int
    attempts: int
    max_attempts: int
    next_run_at: datetime
    lease_expires_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ReapResult(BaseModel):
    """Outcome of one reaper sweep over expired-lease orphan jobs."""
    requeued_ids: list[int] = Field(default_factory=list)
    failed_notebook_ids: list[int] = Field(default_factory=list)  # notebooks to reconcile -> FAILED
