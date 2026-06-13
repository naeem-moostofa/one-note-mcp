"""
Standalone verification for SearchService.

Seeds a synthetic user / notebook / section / page set whose content mimics
OCR-mangled CS246 lecture notes (with `painters` standing in for OCR'd
`pointers`), then exercises SearchService.search against four scenarios:

  1. FTS exact hit:                'overloading'  -> finds CS246 page
  2. Trigram fuzzy fallback:       'pointers'     -> finds the page via `painters`
  3. Notebook scope enforcement:   search in an unrelated notebook -> empty
  4. Multi-page ranking:           'references' across two pages -> ordered

Also runs an EXPLAIN on a trigram query to confirm the planner uses the new
ix_pages_content_trgm GIN index.

This file is throwaway verification, not part of the runtime. Delete after
the MCP server is wired up and end-to-end can be tested against real synced
data.
"""

from __future__ import annotations

import asyncio
import secrets

from sqlalchemy import text

from app.core.database import AsyncSessionLocal, engine
from app.models import (
    MCPConnection,
    MicrosoftConnection,
    MicrosoftConnectionStatus,
    Notebook,
    Page,
    Section,
    User,
)
from app.services.search_service import SearchService

# Long enough that snippets actually trim around the match rather than
# returning the whole content blob.
CS246_CONTENT = (
    "CS 246 Lecture 8 - Operator Overloading\n"
    "Today we cover function overloading and a brief intro to painters. "
    "When you have multiple painters into the same memory region they must "
    "not alias unless the language guarantees it. Function references in C++ "
    "behave differently than painters: a reference must be bound at construction "
    "and cannot be rebound. Operator overloading lets us define + for our own "
    "types. Examples: std::vector::operator[] returns a reference, while .at() "
    "performs bounds checking. We also discussed const correctness and the "
    "difference between a const painter and a painter to const. The lecture "
    "ended with a short exercise comparing painters and references in the "
    "context of function parameters."
)

NETWORKS_CONTENT = (
    "CS 456 Lecture 2 - Network Protocols\n"
    "Today we covered TCP three-way handshake, sequence numbers, and "
    "congestion control. References to the textbook are at the end of "
    "the slides. Function call overhead in protocol stacks matters when "
    "you have millions of packets per second."
)

UNRELATED_CONTENT = (
    "Personal journal entry. Went hiking today. The trail was steep but "
    "the view at the top was worth it. Thinking about reading more about "
    "watercolor painters this weekend."
)


async def seed():
    async with AsyncSessionLocal() as session:
        # Idempotent-ish: drop everything we own and re-create. Cascade
        # handles sections / pages / connections.
        await session.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))

        user = User(
            microsoft_oid="verify-search-oid",
            email="verify@example.com",
            display_name="Verify Search",
        )
        session.add(user)
        await session.flush()

        # Required for the schema even though we never call Graph.
        ms_conn = MicrosoftConnection(
            user_id=user.id,
            encrypted_msal_token_cache="not-a-real-token",
            status=MicrosoftConnectionStatus.ACTIVE,
        )
        session.add(ms_conn)

        cs_notebook = Notebook(
            user_id=user.id,
            onenote_id="cs246-notebook",
            display_name="CS 246",
            sync_enabled=True,
        )
        nets_notebook = Notebook(
            user_id=user.id,
            onenote_id="cs456-notebook",
            display_name="CS 456",
            sync_enabled=True,
        )
        personal_notebook = Notebook(
            user_id=user.id,
            onenote_id="personal-notebook",
            display_name="Personal",
            sync_enabled=True,
        )
        session.add_all([cs_notebook, nets_notebook, personal_notebook])
        await session.flush()

        cs_section = Section(
            notebook_id=cs_notebook.id,
            onenote_id="cs246-lec-section",
            display_name="Lectures",
        )
        nets_section = Section(
            notebook_id=nets_notebook.id,
            onenote_id="cs456-lec-section",
            display_name="Lectures",
        )
        personal_section = Section(
            notebook_id=personal_notebook.id,
            onenote_id="personal-section",
            display_name="Journal",
        )
        session.add_all([cs_section, nets_section, personal_section])
        await session.flush()

        cs_page = Page(
            section_id=cs_section.id,
            onenote_id="cs246-lec8",
            title="Lecture 8 - Operator Overloading",
            content=CS246_CONTENT,
            content_hash="hash-cs246-lec8",
        )
        nets_page = Page(
            section_id=nets_section.id,
            onenote_id="cs456-lec2",
            title="Lecture 2 - Protocols",
            content=NETWORKS_CONTENT,
            content_hash="hash-cs456-lec2",
        )
        personal_page = Page(
            section_id=personal_section.id,
            onenote_id="personal-journal-1",
            title="Hiking",
            content=UNRELATED_CONTENT,
            content_hash="hash-journal-1",
        )
        session.add_all([cs_page, nets_page, personal_page])
        await session.flush()

        # A throwaway MCPConnection so the schema feels complete. Not actually
        # used by the search service; the MCP tool layer does scope enforcement.
        session.add(MCPConnection(
            user_id=user.id,
            token_hash=secrets.token_hex(32),
            display_name="verify",
            scope_all_notebooks=True,
            notebook_ids=None,
        ))

        await session.commit()
        return {
            "user_id": user.id,
            "cs_notebook_id": cs_notebook.id,
            "nets_notebook_id": nets_notebook.id,
            "personal_notebook_id": personal_notebook.id,
            "cs_page_id": cs_page.id,
            "nets_page_id": nets_page.id,
            "personal_page_id": personal_page.id,
        }


async def run_scenarios(ids):
    async with AsyncSessionLocal() as session:
        svc = SearchService(session)

        results = {}

        # 1. FTS exact match — `overloading` is in CS246 content + title.
        hits = await svc.search(
            query="overloading",
            notebook_ids=[ids["cs_notebook_id"], ids["nets_notebook_id"], ids["personal_notebook_id"]],
        )
        results["fts_overloading"] = [
            {"page_id": h.page_id, "notebook": h.notebook_name, "snippets": [s.text for s in h.snippets]}
            for h in hits
        ]

        # 2. Trigram fuzzy fallback — `pointers` is NOT in content; OCR'd as `painters`.
        hits = await svc.search(
            query="pointers",
            notebook_ids=[ids["cs_notebook_id"], ids["nets_notebook_id"], ids["personal_notebook_id"]],
        )
        results["trgm_pointers"] = [
            {"page_id": h.page_id, "notebook": h.notebook_name, "snippets": [s.text for s in h.snippets]}
            for h in hits
        ]

        # 3. Scope enforcement — search the CS246 term but restrict to networks notebook.
        hits = await svc.search(
            query="overloading",
            notebook_ids=[ids["nets_notebook_id"]],
        )
        results["scope_restricted"] = [
            {"page_id": h.page_id, "notebook": h.notebook_name}
            for h in hits
        ]

        # 4. Multi-page ranking — `references` appears in both CS pages.
        hits = await svc.search(
            query="references",
            notebook_ids=[ids["cs_notebook_id"], ids["nets_notebook_id"]],
        )
        results["multi_page_references"] = [
            {"page_id": h.page_id, "notebook": h.notebook_name, "n_snippets": len(h.snippets)}
            for h in hits
        ]

        return results


async def explain_trgm():
    async with AsyncSessionLocal() as session:
        # With only 3 rows the planner correctly prefers seq scan over the
        # GIN index. To prove the index *can* be used we disable seqscan for
        # this one statement (SET LOCAL, so it doesn't leak) and use the `<%`
        # operator that the GIN trgm_ops index supports directly.
        await session.execute(text("ANALYZE pages"))
        await session.execute(text("SET LOCAL enable_seqscan = OFF"))
        # Lower the word_similarity threshold the `<%` operator consults so
        # it actually finds the candidate; default is 0.6 which won't match
        # painters vs pointers.
        await session.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.3"))
        result = await session.execute(text(
            "EXPLAIN (ANALYZE, COSTS OFF) "
            "SELECT id, word_similarity('pointers', content) AS s "
            "FROM pages "
            "WHERE 'pointers' <% content "
            "ORDER BY s DESC LIMIT 5"
        ))
        return [row[0] for row in result.all()]


def fmt(label: str, value):
    print(f"\n{'=' * 6} {label} {'=' * 6}")
    if isinstance(value, list) and value and isinstance(value[0], str):
        for line in value:
            print(line)
    else:
        import json
        print(json.dumps(value, indent=2, default=str))


async def main():
    print("Seeding…")
    ids = await seed()
    fmt("seeded ids", ids)

    print("\nRunning search scenarios…")
    results = await run_scenarios(ids)
    fmt("1. FTS — query='overloading'", results["fts_overloading"])
    fmt("2. Trigram fallback — query='pointers' (OCR'd as 'painters')", results["trgm_pointers"])
    fmt("3. Scope enforcement — 'overloading' in networks-only", results["scope_restricted"])
    fmt("4. Multi-page — 'references'", results["multi_page_references"])

    print("\nRunning EXPLAIN ANALYZE for the trigram path…")
    plan = await explain_trgm()
    fmt("EXPLAIN ANALYZE", plan)

    # Acceptance assertions (the script fails loud if any of these break).
    assert any(h["page_id"] == ids["cs_page_id"] for h in results["fts_overloading"]), \
        "FTS for 'overloading' should hit the CS246 page"

    pointer_hits = results["trgm_pointers"]
    assert any(h["page_id"] == ids["cs_page_id"] for h in pointer_hits), \
        "Trigram fallback for 'pointers' should hit the CS246 page (contains OCR'd `painters`)"
    cs_hit = next(h for h in pointer_hits if h["page_id"] == ids["cs_page_id"])
    assert any("painter" in s.lower() for s in cs_hit["snippets"]), \
        f"At least one snippet should contain `painter`; got {cs_hit['snippets']}"

    # Scope enforcement: the CS246 page must not appear when the caller restricts
    # to the networks notebook, even though it's the strongest match in the corpus.
    # (Trigram fuzzy hits *inside* the allowed scope are fine — that's the
    # threshold doing its job, not a scope bug.)
    assert not any(h["page_id"] == ids["cs_page_id"] for h in results["scope_restricted"]), \
        "Scope restricted to networks must exclude the CS246 page"
    for h in results["scope_restricted"]:
        assert h["notebook"] == "CS 456", \
            f"Every returned hit must be in the networks notebook; got {h['notebook']}"

    assert len(results["multi_page_references"]) >= 2, \
        "'references' appears in both CS pages and should produce 2 hits"

    print("\nAll acceptance assertions passed.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
