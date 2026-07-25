"""
The two OneNote MCP tools, registered against the FastMCP instance in
`app.mcp.server`.

Each tool receives:
  - `session: AsyncSession` injected via `Depends(get_db_session)` — opened,
    committed-or-rolled-back, and closed by FastMCP around the tool body.
  - The resolved MCP scope via `current_scope()`, which reads the verified
    `AccessToken` FastMCP stashed in the request context.

`output_schema=None` is deliberate on both tools: FastMCP otherwise emits every
result twice on the wire, once as a text block and once as structured content.

Tool bodies are therefore just business logic: enforce scope, delegate to a
service, return the lean flat response shape. See
`plans/mcp-toolset-redesign.md`.
"""

from __future__ import annotations

from fastmcp.dependencies import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.auth import current_scope
from app.mcp.deps import get_db_session
from app.mcp.server import mcp
from app.schemas import SearchSnippet
from app.services.search_service import SearchService


@mcp.tool(output_schema=None)
async def onenote_search(
    query: str,
    notebook: str | None = None,
    section: str | None = None,
    session: AsyncSession = Depends(get_db_session),
) -> list[SearchSnippet]:
    """Searches the user's OneNote notes and returns many short snippets, ranked best-first.

    Snippets are deliberately short — long enough to judge which are relevant, not to answer from. Read them, pick the ones that look relevant, and call `onenote_expand_snippets` with their `snippet_id`s to get the surrounding context.

    Do not re-search to get more detail on a page you already found — expand instead. One search is usually enough.

    Searches every notebook the user has enabled by default. Note content mixes typed text with best-effort OCR of handwriting and images, so expect recognition errors; search tolerates them via fuzzy matching.

    Parameters:
    - query (str, required): natural content words, plus any notebook, section or page names you know. A query term matching a page's "Notebook > Section > Page" breadcrumb ranks that page higher, so "STAT231 Chapter 4 confidence interval" beats a bare "confidence interval" when you know where to look. Names match loosely — spacing, abbreviations and small misspellings are tolerated ("STAT 231" and "Ch" both land) — but digits must be exact, since "Chapter 4" and "Chapter 5" are told apart by the number alone. Supports phrase quoting (`"exact phrase"`) and exclusion (`-term`). Common filler words are ignored.
    - notebook (str, optional): hard filter — drops every notebook whose name does not contain this, e.g. "CS241". Naming the notebook in `query` instead only boosts it, which is the better choice when you are not certain the answer lives there.
    - section (str, optional): hard filter on section name, e.g. "Week 3". Same trade-off as `notebook`.

    Each snippet carries a `page` breadcrumb ("Notebook > Section > Page"). `stale: true` means that page is mid-sync and its content may be incomplete.
    """
    scope = current_scope()
    return await SearchService(session).search(
        query=query,
        notebook_ids=scope.allowed_notebook_ids,
        notebook=notebook,
        section=section,
    )


@mcp.tool(output_schema=None)
async def onenote_expand_snippets(
    snippet_ids: list[str],
    session: AsyncSession = Depends(get_db_session),
) -> list[SearchSnippet]:
    """Returns more surrounding context for snippets from a previous `onenote_search`.

    Pass the `snippet_id`s of the snippets you judged relevant — several at once in a single call. Each result comes back with more context and a fresh `snippet_id`, so you can expand the same snippet again if it is still not enough.

    Expand rather than re-searching when you have already found the right page.

    Parameters:
    - snippet_ids (list[str], required): `snippet_id` values from `onenote_search` or from an earlier expansion.

    A snippet whose page changed since your search comes back with `error` set instead of `text`; run `onenote_search` again to get fresh ids.
    """
    scope = current_scope()
    return await SearchService(session).expand_snippets(
        snippet_ids=snippet_ids,
        allowed_notebook_ids=scope.allowed_notebook_ids,
    )
