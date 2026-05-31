"""
The three OneNote MCP tools, registered against the FastMCP instance in
`app.mcp.server`.

Each tool receives:
  - `session: AsyncSession` injected via `Depends(get_db_session)` — opened,
    committed-or-rolled-back, and closed by FastMCP around the tool body.
  - The resolved MCP scope via `current_scope()`, which reads the verified
    `AccessToken` FastMCP stashed in the request context.

Tool bodies are therefore just business logic: enforce scope, delegate to a
service or repo, project to the lean MCP response shape.
"""

from __future__ import annotations

from fastmcp.dependencies import Depends
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.auth import current_scope
from app.mcp.deps import get_db_session
from app.mcp.server import mcp
from app.models import NotebookSyncStatus, PageSyncStatus
from app.repositories.page_repository import PageRepository
from app.schemas import NotebookSummary, PageContent, SearchHit
from app.services.notebook_service import NotebookService
from app.services.search_service import SearchService


def _is_stale(
    page_status: PageSyncStatus | None,
    notebook_status: NotebookSyncStatus | None,
) -> bool:
    if page_status in (PageSyncStatus.SYNCING, PageSyncStatus.FAILED):
        return True
    if notebook_status in (NotebookSyncStatus.SYNCING, NotebookSyncStatus.FAILED):
        return True
    return False


@mcp.tool
async def onenote_list_notebooks(
    session: AsyncSession = Depends(get_db_session),
) -> list[NotebookSummary]:
    """Lists the OneNote notebooks this MCP connection can see.

    Returns each notebook's id and display_name. Call this first in a new
    session so you have the notebook IDs needed to scope onenote_search_pages.
    Only notebooks the user has enabled for sync are returned — others won't
    appear here at all, so an empty response means there's nothing searchable.
    """
    scope = current_scope()
    return await NotebookService(session).list_for_user(
        user_id=scope.user_id,
        filter_notebook_ids=scope.allowed_notebook_ids,
    )


@mcp.tool
async def onenote_search_pages(
    query: str,
    notebook_ids: list[int],
    search_size: int = 80,
    max_pages: int = 10,
    max_snippets_per_page: int = 5,
    session: AsyncSession = Depends(get_db_session),
) -> list[SearchHit]:
    """Searches OneNote pages whose content matches `query`, returning relevance-ranked hits with title, section, notebook, and content snippets. `notebook_ids` is required — get IDs from onenote_list_notebooks first.

    Page content mixes verbatim typed text with best-effort OCR of handwritten and image content. OCR portions may contain recognition errors (e.g. `painters` for `Pointers`); search uses fuzzy matching to tolerate them. Snippets are character windows around matches, not sentences — expect mid-sentence cuts.

    Prefer narrow, targeted queries over broad ones. If a snippet doesn't give you enough context, either re-call with a larger `search_size` (up to 250) or call onenote_get_page(page_id) for the full combined content.

    `stale: true` on a hit means the page or its notebook is mid-sync — content may be incomplete.

    Parameters:
    - query (str, required): the search text. Supports phrase quoting (`"exact phrase"`) and exclusion (`-term`).
    - notebook_ids (list[int], required): notebooks to search. Obtain from onenote_list_notebooks.
    - search_size (int, default 80, max 250): characters of context on each side of a match.
    - max_pages (int, default 10, max 20): cap on pages returned.
    - max_snippets_per_page (int, default 5, max 10): cap on snippets per page.
    """
    scope = current_scope()
    allowed = set(scope.allowed_notebook_ids)
    scoped_notebook_ids = [nb_id for nb_id in notebook_ids if nb_id in allowed]
    if not scoped_notebook_ids:
        return []
    return await SearchService(session).search(
        query=query,
        notebook_ids=scoped_notebook_ids,
        search_size=search_size,
        max_pages=max_pages,
        max_snippets_per_page=max_snippets_per_page,
    )


@mcp.tool
async def onenote_get_page(
    page_id: int,
    session: AsyncSession = Depends(get_db_session),
) -> PageContent:
    """Fetches the full combined content of a single OneNote page — typed text and OCR text interleaved in visual reading order. Use this when a snippet from onenote_search_pages lacks enough context to answer.

    Same content warning as onenote_search_pages: OCR portions may contain recognition errors. Reading order is best-effort, not pixel-faithful.

    `stale: true` means the page or its notebook is mid-sync — content may be incomplete.

    Parameters:
    - page_id (int, required): obtained from a SearchHit returned by onenote_search_pages.
    """
    scope = current_scope()
    detail = await PageRepository(session).get_with_context(page_id)
    if detail is None or detail.notebook_id not in scope.allowed_notebook_ids:
        # Treat out-of-scope and not-found the same so guessable IDs don't
        # leak existence to the caller.
        raise ToolError(f"Page {page_id} not found")
    return PageContent(
        page_title=detail.page_title,
        section_name=detail.section_name,
        notebook_name=detail.notebook_name,
        content=detail.content or "",
        stale=_is_stale(detail.page_sync_status, detail.notebook_sync_status),
    )
