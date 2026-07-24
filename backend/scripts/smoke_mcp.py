"""
End-to-end smoke test for the MCP layer's underlying services.

Seeds a minimal corpus (1 user, 2 notebooks, 1 section each, 2 pages), then
exercises MCPConnectionService → SearchService →
PageRepository.get_with_context. Cleans up at the end.

Verifies:
  - Raw-token round-trip: create → hash → resolve → same scope
  - Scope intersection: revoked tokens fail, out-of-scope notebooks filtered
  - The fixed `get_with_context` runs (the Page.last_synced_at bug is gone)
  - Search returns flat, capped, self-contained snippets
  - Snippet expansion: chainable, scope-enforced, stale-handle detection

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
from app.services.search_service import MAX_SNIPPET_CHARS, SearchService, encode_handle

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

        # Long enough, and split into blocks, that a snippet covers only part of
        # the page — otherwise expansion has nowhere to grow and the chainable
        # -handle checks below are vacuous.
        pointers_page = Page(
            section_id=lecture_section.id,
            onenote_id="pg-1",
            title="Pointers",
            content=(
                "A pointer holds the address of another variable. Pointer arithmetic in C "
                "lets you traverse arrays without indexing them directly.\n\n"
                "Declaring one is `int *p = &x;` — the star belongs to the variable, not the "
                "type, which is why `int* a, b;` declares one pointer and one plain int. "
                "Dereferencing with `*p` reads through to the pointed-at storage.\n\n"
                "The null pointer is the one address guaranteed not to name an object. "
                "Dereferencing it is undefined behaviour, not a guaranteed crash, which is "
                "why the compiler is free to delete a null check that appears after a "
                "dereference of the same variable.\n\n"
                "Array decay: an array expression converts to a pointer to its first element "
                "in almost every context, so `sizeof` inside a callee no longer sees the "
                "array. Pass the length alongside the pointer or the callee cannot know it.\n\n"
                "A dangling pointer outlives the storage it names — returning the address of "
                "a local, or reading through a pointer after free(). The value still looks "
                "like an address and nothing on the pointer itself records that it went bad."
            ),
        )
        memory_page = Page(
            section_id=lecture_section.id,
            onenote_id="pg-2",
            title="Memory",
            content="Stack vs heap allocation; manual free() is required for malloc'd buffers.",
            sync_status=PageSyncStatus.SYNCING,  # exercises the stale path
        )
        grocery_page = Page(
            section_id=notes_section.id,
            onenote_id="pg-3",
            title="Grocery list",
            content="Apples, oranges, bread, butter.",
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

    # 2. SearchService.search via the same path the MCP tool will take.
    async with AsyncSessionLocal() as session:
        snippets = await SearchService(session).search(
            query="pointer",
            notebook_ids=scope.allowed_notebook_ids,
        )
    _assert(len(snippets) >= 1, f"search('pointer') returned snippets ({len(snippets)})")
    pointer = next((s for s in snippets if "Pointers" in s.page), None)
    _assert(pointer is not None, "the Pointers page is among the snippets")
    assert pointer is not None
    _assert(pointer.page == "CS 246 > Lecture 4 > Pointers", f"breadcrumb is notebook > section > page, got {pointer.page!r}")
    _assert(pointer.snippet_id != "", "snippet carries an expansion handle")
    _assert(
        max(len(s.text or "") for s in snippets) <= MAX_SNIPPET_CHARS,
        f"no snippet exceeds the {MAX_SNIPPET_CHARS}-char cap",
    )
    _assert("stale" not in pointer.model_dump(), "stale is omitted when false")
    _assert("error" not in pointer.model_dump(), "error is omitted when unset")

    # 3. Name filters resolve without the caller looking up ids first.
    async with AsyncSessionLocal() as session:
        filtered = await SearchService(session).search(
            query="pointer",
            notebook_ids=scope.allowed_notebook_ids,
            notebook="CS 246",
        )
    _assert(all("CS 246" in s.page for s in filtered), "notebook name filter keeps only CS 246 snippets")

    # 4. Snippets from notebook B (which is mid-sync) → stale: True.
    async with AsyncSessionLocal() as session:
        personal = await SearchService(session).search(
            query="apples",
            notebook_ids=[ids["personal_notebook"]],
        )
    _assert(len(personal) >= 1, "search in notebook B found the grocery page")
    _assert(personal[0].stale is True, "grocery snippet is stale because notebook B is SYNCING")
    _assert(personal[0].model_dump().get("stale") is True, "stale IS serialized when true")

    # 4b. Expansion: chainable, scope-enforced, and stale-handle detection.
    async with AsyncSessionLocal() as session:
        service = SearchService(session)
        expanded = await service.expand_snippets([pointer.snippet_id], scope.allowed_notebook_ids)
        _assert(len(expanded) == 1, "expanding one snippet returns one result")
        _assert(expanded[0].error is None, f"expansion succeeded: {expanded[0].error}")
        _assert(
            len(expanded[0].text or "") >= len(pointer.text or ""),
            "expanded snippet is at least as long as the original",
        )
        _assert(
            expanded[0].snippet_id != pointer.snippet_id,
            "expansion returns a fresh handle so it can be expanded again",
        )

        again = await service.expand_snippets([expanded[0].snippet_id], scope.allowed_notebook_ids)
        _assert(again[0].error is None, "the fresh handle expands again (chainable)")

        # A handle whose page is outside the caller's scope must not resolve.
        out_of_scope = await service.expand_snippets([pointer.snippet_id], [ids["personal_notebook"]])
        _assert(
            out_of_scope[0].error is not None and out_of_scope[0].text is None,
            "a handle outside the connection's scope is refused",
        )

        # A handle minted against different content must be rejected, not silently mis-sliced.
        forged = encode_handle(ids["page_pointers"], 0, 20, "content that never existed")
        stale_result = await service.expand_snippets([forged], scope.allowed_notebook_ids)
        _assert(
            stale_result[0].error is not None and "changed" in stale_result[0].error,
            "a handle with a stale content checksum reports the page changed",
        )

        garbage = await service.expand_snippets(["!!!not-a-handle!!!"], scope.allowed_notebook_ids)
        _assert(garbage[0].error is not None, "a malformed handle returns an error, not an exception")

        batch = await service.expand_snippets(
            [pointer.snippet_id, "!!!bad!!!"], scope.allowed_notebook_ids
        )
        _assert(len(batch) == 2, "batch returns one entry per requested id")
        _assert(batch[0].error is None and batch[1].error is not None, "batch preserves request order")

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
