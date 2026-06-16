"""
End-to-end smoke test for the MCP layer's underlying services.

Seeds a minimal corpus (1 user, 2 notebooks, 1 section each, 2 pages), then
exercises MCPConnectionService → NotebookService → SearchService →
PageRepository.get_with_context. Cleans up at the end.

Verifies:
  - Raw-token round-trip: create → hash → resolve → same scope
  - Scope intersection: revoked tokens fail, out-of-scope notebooks filtered
  - The fixed `get_with_context` runs (the Page.last_synced_at bug is gone)
  - SearchService end-to-end still works

Usage:
    uv run python -m scripts.smoke_mcp
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.database import AsyncSessionLocal, engine
from app.models import (
    MCPConnection,
    Notebook,
    NotebookSyncStatus,
    Page,
    PageSyncStatus,
    Section,
    User,
)
from app.mcp.auth import MCPConnectionTokenVerifier
from app.repositories.page_repository import PageRepository
from app.services.mcp_connection_service import MCPConnectionService
from app.services.notebook_service import NotebookService
from app.services.search_service import SearchService

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("smoke_mcp")


async def _seed():
    """Insert a minimal corpus. Returns the created IDs."""
    async with AsyncSessionLocal() as session:
        user = User(microsoft_oid="smoke-oid", email="smoke@example.com", display_name="Smoke User")
        session.add(user)
        await session.flush()

        cs_notebook = Notebook(user_id=user.id, onenote_id="nb-a", display_name="CS 246", sync_enabled=True)
        personal_notebook = Notebook(user_id=user.id, onenote_id="nb-b", display_name="Personal", sync_enabled=True)
        archive_notebook = Notebook(
            user_id=user.id,
            onenote_id="nb-c",
            display_name="Archive",
            sync_enabled=False,  # not searchable; should be filtered out of scope
        )
        session.add_all([cs_notebook, personal_notebook, archive_notebook])
        await session.flush()

        lecture_section = Section(notebook_id=cs_notebook.id, onenote_id="sec-a", display_name="Lecture 4")
        notes_section = Section(notebook_id=personal_notebook.id, onenote_id="sec-b", display_name="Notes")
        session.add_all([lecture_section, notes_section])
        await session.flush()

        pointers_page = Page(
            section_id=lecture_section.id,
            onenote_id="pg-1",
            title="Pointers",
            content="A pointer holds the address of another variable. Pointer arithmetic in C lets you traverse arrays.",
            content_hash="h1",
        )
        memory_page = Page(
            section_id=lecture_section.id,
            onenote_id="pg-2",
            title="Memory",
            content="Stack vs heap allocation; manual free() is required for malloc'd buffers.",
            content_hash="h2",
            sync_status=PageSyncStatus.SYNCING,  # exercises the stale path
        )
        grocery_page = Page(
            section_id=notes_section.id,
            onenote_id="pg-3",
            title="Grocery list",
            content="Apples, oranges, bread, butter.",
            content_hash="h3",
        )
        session.add_all([pointers_page, memory_page, grocery_page])
        # Notebook B is mid-sync — every hit/page in it should be `stale: True`.
        personal_notebook.sync_status = NotebookSyncStatus.SYNCING
        personal_notebook.last_synced_at = datetime.now(timezone.utc)

        await session.commit()
        return {
            "user_id": user.id,
            "cs_notebook": cs_notebook.id,
            "personal_notebook": personal_notebook.id,
            "archive_notebook": archive_notebook.id,
            "page_pointers": pointers_page.id,
            "page_memory_syncing": memory_page.id,
            "page_grocery": grocery_page.id,
        }


async def _teardown(user_id: int):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, user_id)
        if user:
            await session.delete(user)
        await session.commit()


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise SystemExit(f"FAIL: {message}")
    log.info("  OK: %s", message)


async def _run(ids: dict[str, int]) -> None:
    # 1. Create + resolve a connection scoped to all notebooks.
    async with AsyncSessionLocal() as session:
        created = await MCPConnectionService(session).create(
            user_id=ids["user_id"],
            scope_all_notebooks=True,
            display_name="smoke test",
        )
        await session.commit()
    conn_id = created.id
    raw_token = created.raw_token
    _assert(created.scope_all_notebooks is True, "MCPConnectionCreatedResponse echoes the scope_all_notebooks flag")
    _assert(created.notebook_ids is None, "scope_all_notebooks=True → notebook_ids omitted in the response")
    _assert(created.display_name == "smoke test", "display_name round-trips")
    log.info("Created connection %d with raw token len=%d", conn_id, len(raw_token))

    async with AsyncSessionLocal() as session:
        scope = await MCPConnectionService(session).resolve_token(raw_token)
        await session.commit()
    _assert(scope is not None, "resolve_token returned a scope")
    assert scope is not None
    _assert(scope.user_id == ids["user_id"], "scope.user_id matches the owning user")
    _assert(
        set(scope.allowed_notebook_ids) == {ids["cs_notebook"], ids["personal_notebook"]},
        "scope_all_notebooks intersects with sync_enabled — excludes the disabled notebook",
    )

    # 2. NotebookService.list_enabled_summaries reflects the same intersection
    #    when the caller supplies the scope's allowed_notebook_ids as the filter.
    async with AsyncSessionLocal() as session:
        notebooks = await NotebookService(session).list_enabled_summaries(
            user_id=scope.user_id,
            filter_notebook_ids=scope.allowed_notebook_ids,
        )
    names = sorted(n.display_name for n in notebooks)
    _assert(names == ["CS 246", "Personal"], f"list_enabled_summaries(filter=…) returned {names!r}")

    # Also verify the service is scope-blind: no filter = all sync-enabled.
    async with AsyncSessionLocal() as session:
        all_synced = await NotebookService(session).list_enabled_summaries(user_id=scope.user_id)
    all_names = sorted(n.display_name for n in all_synced)
    _assert(all_names == ["CS 246", "Personal"], f"list_enabled_summaries(no filter) returned {all_names!r}")
    _assert(
        all(set(vars(n).keys()) == {"id", "display_name"} for n in notebooks),
        "NotebookSummary carries only id + display_name (no sync_status / last_synced_at)",
    )

    # 3. SearchService.search via the same path the MCP tool will take.
    async with AsyncSessionLocal() as session:
        hits = await SearchService(session).search(
            query="pointer",
            notebook_ids=scope.allowed_notebook_ids,
            search_size=80,
            max_pages=5,
            max_snippets_per_page=3,
        )
    _assert(len(hits) >= 1, f"search('pointer') returned hits ({len(hits)})")
    pointer_hit = next((h for h in hits if h.page_id == ids["page_pointers"]), None)
    _assert(pointer_hit is not None, "the Pointers page is among the hits")
    assert pointer_hit is not None
    _assert(pointer_hit.notebook_name == "CS 246", "hit's notebook_name is CS 246")
    _assert(pointer_hit.section_name == "Lecture 4", "hit's section_name is Lecture 4")
    _assert(not hasattr(pointer_hit, "notebook_id") or "notebook_id" not in pointer_hit.model_dump(), "SearchHit no longer carries notebook_id")
    _assert(len(pointer_hit.snippets) > 0, "hit has at least one snippet")
    _assert(
        "start_offset" not in pointer_hit.snippets[0].model_dump(),
        "SearchSnippet no longer carries start_offset",
    )
    _assert(pointer_hit.stale is False, "the Pointers hit is not stale (notebook + page healthy)")

    # 4. Hit from notebook B (which is mid-sync) → stale: True.
    async with AsyncSessionLocal() as session:
        personal_hits = await SearchService(session).search(
            query="apples",
            notebook_ids=[ids["personal_notebook"]],
            search_size=80,
            max_pages=5,
            max_snippets_per_page=3,
        )
    _assert(len(personal_hits) == 1, "search in notebook B found the grocery page")
    _assert(personal_hits[0].stale is True, "grocery hit is stale because notebook B is SYNCING")

    # 5. get_with_context — the previously buggy method, now fixed.
    async with AsyncSessionLocal() as session:
        detail = await PageRepository(session).get_with_context(ids["page_pointers"])
    _assert(detail is not None, "get_with_context returned the page")
    assert detail is not None
    _assert(detail.page_title == "Pointers", "detail.page_title is Pointers")
    _assert(detail.section_name == "Lecture 4", "detail.section_name is Lecture 4")
    _assert(detail.notebook_name == "CS 246", "detail.notebook_name is CS 246")
    _assert(detail.content is not None and "pointer" in detail.content.lower(), "detail.content includes the page text")
    _assert(detail.notebook_last_synced_at is None, "CS 246 has not been synced, so last_synced_at is None")

    async with AsyncSessionLocal() as session:
        personal_detail = await PageRepository(session).get_with_context(ids["page_grocery"])
    _assert(personal_detail is not None, "get_with_context returned the grocery page")
    assert personal_detail is not None
    _assert(
        personal_detail.notebook_sync_status == NotebookSyncStatus.SYNCING,
        "notebook_sync_status comes through from the notebook (the bug-fix verification)",
    )
    _assert(
        personal_detail.notebook_last_synced_at is not None,
        "notebook_last_synced_at is populated from Notebook.last_synced_at (the column that actually exists)",
    )

    # 6. TokenVerifier — the FastMCP-blessed auth path.
    verifier = MCPConnectionTokenVerifier()
    access = await verifier.verify_token(raw_token)
    _assert(access is not None, "TokenVerifier.verify_token returns an AccessToken for a valid raw token")
    assert access is not None
    _assert(access.token == raw_token, "AccessToken.token round-trips the raw token")
    _assert(access.client_id == str(ids["user_id"]), "AccessToken.client_id is the owning user id (str)")
    _assert(access.scopes == [], "AccessToken.scopes is empty — we don't model OAuth scopes")
    _assert(
        set(access.claims["onenote_mcp_allowed_notebook_ids"]) == {ids["cs_notebook"], ids["personal_notebook"]},
        "AccessToken.claims carries the resolved notebook scope",
    )
    _assert(
        access.claims["onenote_mcp_connection_id"] == conn_id,
        "AccessToken.claims carries the connection id",
    )

    bad_access = await verifier.verify_token("onmcp_definitely-not-a-real-token")
    _assert(bad_access is None, "TokenVerifier returns None for unknown tokens (FastMCP responds 401)")

    # 7. Revoke flow.
    async with AsyncSessionLocal() as session:
        await MCPConnectionService(session).revoke(user_id=ids["user_id"], connection_id=conn_id)
        await session.commit()
    async with AsyncSessionLocal() as session:
        revoked_scope = await MCPConnectionService(session).resolve_token(raw_token)
    _assert(revoked_scope is None, "revoked tokens no longer resolve via the service")

    revoked_access = await verifier.verify_token(raw_token)
    _assert(revoked_access is None, "TokenVerifier returns None for revoked tokens too")

    # 8. Out-of-scope (notebook_ids not in connection scope) — caller-side check.
    async with AsyncSessionLocal() as session:
        narrow_created = await MCPConnectionService(session).create(
            user_id=ids["user_id"],
            scope_all_notebooks=False,
            notebook_ids=[ids["cs_notebook"]],
            display_name="scoped to CS 246 only",
        )
        await session.commit()
    _assert(narrow_created.scope_all_notebooks is False, "narrow connection has scope_all_notebooks=False")
    _assert(narrow_created.notebook_ids == [ids["cs_notebook"]], "narrow connection echoes the requested notebook_ids in the response")
    async with AsyncSessionLocal() as session:
        narrow_scope = await MCPConnectionService(session).resolve_token(narrow_created.raw_token)
        await session.commit()
    assert narrow_scope is not None
    _assert(
        narrow_scope.allowed_notebook_ids == [ids["cs_notebook"]],
        "narrow connection only allows CS 246",
    )


async def main():
    log.info("Seeding…")
    ids = await _seed()
    try:
        log.info("Running smoke checks…")
        await _run(ids)
        log.info("ALL CHECKS PASSED")
    finally:
        log.info("Tearing down…")
        await _teardown(ids["user_id"])
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
