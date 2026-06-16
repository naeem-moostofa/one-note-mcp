"""
Print EXPLAIN ANALYZE for the two `search_trgm` query shapes — current
(word_similarity in WHERE) vs. proposed (`<%` in WHERE + word_similarity for
ranking) — so we can see what the planner does in each case.

Uses raw SQL strings rather than SQLAlchemy compile() because compile()'s
literal_binds escapes `%` to `%%` for pyformat paramstyle, which collides
with the `<%` operator. The repository's actual SQLAlchemy expression is
fine at runtime — asyncpg handles it correctly via prepared statements,
not literal-binding. This is a quirk of dumping the SQL to a string,
not a runtime concern.

Usage:
    uv run python -m scripts.explain_search_trgm pointers
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import func, select, text

from app.core.database import AsyncSessionLocal, engine
from app.models import Notebook, Page


def current_sql(terms: list[str], notebook_ids: list[int]) -> str:
    # The shape my repo emits today.
    quoted_terms = ", ".join(f"'{t}'" for t in terms)
    greatest = (
        ", ".join(f"word_similarity('{t}', p.content)" for t in terms)
    )
    score = f"GREATEST({greatest})" if len(terms) > 1 else f"word_similarity('{terms[0]}', p.content)"
    return f"""
SELECT p.id, {score} AS score, p.content
FROM pages p
JOIN sections s ON p.section_id = s.id
WHERE s.notebook_id = ANY(ARRAY{notebook_ids})
  AND p.content IS NOT NULL
  AND {score} >= 0.3
ORDER BY {score} DESC
LIMIT 30
""".strip()


def proposed_sql(terms: list[str], notebook_ids: list[int]) -> str:
    # The shape the change will produce: `<%` for GIN pickup, word_similarity for ranking.
    greatest = (
        ", ".join(f"word_similarity('{t}', p.content)" for t in terms)
    )
    score = f"GREATEST({greatest})" if len(terms) > 1 else f"word_similarity('{terms[0]}', p.content)"
    indexed = " OR ".join(f"'{t}' <% p.content" for t in terms)
    return f"""
SELECT p.id, {score} AS score, p.content
FROM pages p
JOIN sections s ON p.section_id = s.id
WHERE s.notebook_id = ANY(ARRAY{notebook_ids})
  AND p.content IS NOT NULL
  AND ({indexed})
ORDER BY {score} DESC
LIMIT 30
""".strip()


async def explain(label: str, sql: str, *, force_index: bool = False, set_threshold: float | None = None):
    print(f"\n{'=' * 6} {label} {'=' * 6}")
    print(f"-- SQL:\n{sql}\n")
    async with AsyncSessionLocal() as session:
        await session.execute(text("ANALYZE pages"))
        if set_threshold is not None:
            await session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {set_threshold}"))
        if force_index:
            await session.execute(text("SET LOCAL enable_seqscan = OFF"))
        result = await session.execute(text(f"EXPLAIN (ANALYZE, COSTS OFF) {sql}"))
        for row in result.all():
            print(row[0])
        await session.rollback()


async def main():
    terms = sys.argv[1:] or ["pointers"]
    print(f"Terms: {terms}")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Notebook.id))
        notebook_ids = [row[0] for row in result.all()]
        if not notebook_ids:
            print("No notebooks in DB. Seed first.")
            return
        result = await session.execute(select(func.count()).select_from(Page))
        page_count = result.scalar_one()
    print(f"Notebooks in scope: {notebook_ids}")
    print(f"Total pages in DB: {page_count}")

    cur = current_sql(terms, notebook_ids)
    new = proposed_sql(terms, notebook_ids)

    await explain("CURRENT — planner free", cur)
    await explain("CURRENT — enable_seqscan=OFF (no GIN path reachable)", cur, force_index=True)
    await explain("PROPOSED — planner free, threshold=0.3", new, set_threshold=0.3)
    await explain("PROPOSED — enable_seqscan=OFF, threshold=0.3 (GIN path)", new, force_index=True, set_threshold=0.3)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
