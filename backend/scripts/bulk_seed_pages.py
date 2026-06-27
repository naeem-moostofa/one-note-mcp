"""
Bulk-insert thousands of synthetic pages into the existing notebooks so we
can observe what the Postgres planner does for `search_trgm` at realistic
volume. Each page gets randomized content drawn from a small corpus of
sentence fragments so trigram statistics are roughly representative of
OCR'd lecture notes.

Idempotent in the sense that it never re-creates rows with the same
onenote_id, but it appends — running twice doubles the row count up to
the requested target.

Usage:
    uv run python -m scripts.bulk_seed_pages 5000
"""

from __future__ import annotations

import asyncio
import random
import sys

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal, engine
from app.models import Page, Section


# Neutral content with no trigram overlap with "pointers" / "painters".
# The goal: keep the bulk of the synthetic corpus from accidentally
# matching the query term we use in EXPLAIN tests, so the GIN index can
# meaningfully narrow candidates.
SENTENCE_FRAGMENTS = [
    "the orchestra rehearsed brahms in the upper rotunda all morning",
    "fermentation requires careful temperature control and steady salinity",
    "the mountain ridge curved gently toward the horizon under thin clouds",
    "she folded the dough into thirds and chilled it overnight in the fridge",
    "the lighthouse keeper recorded wave heights every six hours by lamplight",
    "monsoon season brings heavy rainfall and rapid river swelling downstream",
    "espresso extraction depends on grind size, dose, and water temperature",
    "the constellation cassiopeia is visible from northern latitudes year round",
    "kelp forests shelter sea otters and provide habitat for juvenile rockfish",
    "she pressed the linen sheets with lavender water before storing them",
    "trade winds blow steadily westward across the equatorial pacific belt",
    "the sourdough starter was bubbling vigorously after a long room-temperature rise",
    "blue herons fish patiently in the shallows during the early morning hours",
    "the cellist tuned her instrument quietly before the chamber rehearsal began",
    "alpine meadows bloom briefly each summer with anemones and forget-me-nots",
]


async def main():
    target = int(sys.argv[1] if len(sys.argv) > 1 else 5000)

    async with AsyncSessionLocal() as session:
        # Get existing sections we can attach pages to.
        sections = (await session.execute(select(Section.id, Section.notebook_id))).all()
        if not sections:
            print("No sections found — run scripts.verify_search_service first to seed the base structure.")
            return
        section_ids = [s.id for s in sections]

        current_count = (await session.execute(select(func.count()).select_from(Page))).scalar_one()
        to_add = max(0, target - current_count)
        if to_add == 0:
            print(f"Already at or above target ({current_count} >= {target}). Nothing to do.")
            return

        print(f"Pages currently: {current_count}. Adding {to_add} synthetic pages…")
        random.seed(20260529)

        batch_size = 500
        added = 0
        next_idx = current_count  # used to keep onenote_ids unique

        while added < to_add:
            this_batch = min(batch_size, to_add - added)
            rows = []
            for _ in range(this_batch):
                # 5–25 sentence fragments per page, joined with spaces, with
                # the index baked in so the onenote_id stays unique. The
                # content size + token mix is comparable to typed OneNote
                # text (not full OCR — that would be 20-40 KB; we just need
                # enough trigram diversity for the planner to take the index
                # seriously).
                n_frag = random.randint(5, 25)
                content = " ".join(random.choices(SENTENCE_FRAGMENTS, k=n_frag))
                rows.append({
                    "section_id": random.choice(section_ids),
                    "onenote_id": f"synthetic-{next_idx}",
                    "title": f"Synthetic page {next_idx}",
                    "content": content,
                })
                next_idx += 1
            await session.execute(Page.__table__.insert(), rows)
            await session.commit()
            added += this_batch
            print(f"  +{this_batch}  (running total: {current_count + added})")

        final = (await session.execute(select(func.count()).select_from(Page))).scalar_one()
        print(f"Done. Pages now: {final}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
