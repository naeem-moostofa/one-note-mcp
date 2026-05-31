from sqlalchemy import delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook, Page, Section
from app.schemas import (
    PageCreate,
    PageDetailResponse,
    PageFTSHit,
    PageResponse,
    PageSearchQuery,
    PageSearchResponse,
    PageTrgmHit,
    PageUpdate,
    PageWithPath,
    PaginatedResponse,
)


class PageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, page_id: int) -> PageResponse | None:
        row = await self.session.get(Page, page_id)
        return PageResponse.model_validate(row) if row else None

    async def get_by_onenote_id(self, section_id: int, onenote_id: str) -> PageResponse | None:
        row = await self.session.scalar(
            select(Page).where(Page.section_id == section_id, Page.onenote_id == onenote_id)
        )
        return PageResponse.model_validate(row) if row else None

    async def get_with_context(self, page_id: int) -> PageDetailResponse | None:
        """Single-page lookup joined with its section and notebook.

        Returns full content, the section + notebook the page belongs to, both
        sync statuses, and the notebook's `last_synced_at` (pages don't carry
        their own — they're synced as part of a notebook run). Consumed by the
        MCP `onenote_get_page` tool (projected to `PageContent`) and by any
        future REST page-detail endpoint.
        """
        statement = (
            select(
                Page.id.label("page_id"),
                Page.onenote_id,
                Page.title.label("page_title"),
                Page.content,
                Page.content_hash,
                Page.sync_status.label("page_sync_status"),
                Section.display_name.label("section_name"),
                Notebook.id.label("notebook_id"),
                Notebook.display_name.label("notebook_name"),
                Notebook.sync_status.label("notebook_sync_status"),
                Notebook.last_synced_at.label("notebook_last_synced_at"),
            )
            .join(Section, Page.section_id == Section.id)
            .join(Notebook, Section.notebook_id == Notebook.id)
            .where(Page.id == page_id)
        )
        row = (await self.session.execute(statement)).one_or_none()
        if row is None:
            return None
        return PageDetailResponse.model_validate(row)

    async def search(self, data: PageSearchQuery) -> PaginatedResponse[PageSearchResponse]:
        ts_query = func.plainto_tsquery("english", data.query)
        filters = [
            Page.search_vector.op("@@")(ts_query),
            Notebook.id.in_(data.notebook_ids),
        ]

        count_statement = (
            select(func.count())
            .select_from(Page)
            .join(Section, Page.section_id == Section.id)
            .join(Notebook, Section.notebook_id == Notebook.id)
            .where(*filters)
        )

        search_statement = (
            select(
                Page.id,
                Page.onenote_id,
                Page.title,
                Page.sync_status,
                Section.display_name.label("section_name"),
                Notebook.display_name.label("notebook_name"),
                func.ts_headline("english", Page.content, ts_query, "MaxWords=50, MinWords=20, StartSel='', StopSel=''").label("content_excerpt"),
            )
            .join(Section, Page.section_id == Section.id)
            .join(Notebook, Section.notebook_id == Notebook.id)
            .where(*filters)
            .order_by(func.ts_rank(Page.search_vector, ts_query).desc())
            .limit(data.limit)
            .offset(data.offset)
        )

        total = await self.session.scalar(count_statement) or 0
        result = await self.session.execute(search_statement)

        return PaginatedResponse(
            data=[PageSearchResponse.model_validate(row) for row in result.all()],
            total=total,
            limit=data.limit,
            offset=data.offset,
        )

    async def search_fts(
        self,
        notebook_ids: list[int],
        query: str,
        limit: int,
    ) -> list[PageFTSHit]:
        """First-pass full-text search. Returns matching pages with ts_rank_cd scores and content."""
        if not query.strip() or not notebook_ids:
            return []

        # websearch_to_tsquery is the safest variant for arbitrary user input —
        # it tolerates unbalanced quotes, supports phrase quoting, and never errors
        # on stray operators the way to_tsquery does.
        ts_query = func.websearch_to_tsquery("english", query)
        rank = func.ts_rank_cd(Page.search_vector, ts_query)

        statement = (
            select(Page.id, rank.label("rank"), Page.content)
            .join(Section, Page.section_id == Section.id)
            .where(
                Section.notebook_id.in_(notebook_ids),
                Page.search_vector.op("@@")(ts_query),
                Page.content.isnot(None),
            )
            .order_by(rank.desc())
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return [
            PageFTSHit(page_id=row.id, rank=float(row.rank), content=row.content)
            for row in result.all()
        ]

    async def search_trgm(
        self,
        notebook_ids: list[int],
        terms: list[str],
        threshold: float,
        limit: int,
    ) -> list[PageTrgmHit]:
        """
        Trigram fuzzy fallback. For each candidate page, scores against the
        per-term max of `word_similarity(term, content)`. `word_similarity`
        measures the best-matching substring of `content` regardless of how
        long the page is — exactly what we want when the query term is a
        short OCR'd word inside many KB of page content.

        The WHERE clause uses the `<%` operator (word_similarity above the
        session GUC threshold), which the planner can answer using the
        ix_pages_content_trgm GIN index — at scale this turns an O(pages)
        scan into an O(candidates) lookup. `word_similarity(...)` stays in
        the SELECT/ORDER BY so the score is exact and ranking is unchanged.

        `set_config('pg_trgm.word_similarity_threshold', t, true)` aligns the
        GUC the `<%` operator reads with the caller-supplied `threshold`. The
        third argument `is_local = true` scopes the override to the current
        transaction — same effect as `SET LOCAL`, but `set_config` is a normal
        function-call SELECT and accepts bound parameters, whereas `SET LOCAL`
        is a utility statement that Postgres won't parameterize through the
        extended-query protocol asyncpg uses.
        """
        if not terms or not notebook_ids:
            return []

        await self.session.execute(
            text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, true)")
            .bindparams(t=str(threshold))
        )

        sim_exprs = [func.word_similarity(term, Page.content) for term in terms]
        # GREATEST takes 2+ args; collapse to the single expression when there's one term.
        max_sim = func.greatest(*sim_exprs) if len(sim_exprs) > 1 else sim_exprs[0]

        # `term <% content` (LHS is the short query, RHS is the long content)
        # is the form supported by the GIN trgm_ops index. Multiple terms
        # OR-combine into a BitmapOr plan.
        indexed_filters = [literal(term).op("<%")(Page.content) for term in terms]
        indexed_filter = or_(*indexed_filters) if len(indexed_filters) > 1 else indexed_filters[0]

        statement = (
            select(Page.id, max_sim.label("score"), Page.content)
            .join(Section, Page.section_id == Section.id)
            .where(
                Section.notebook_id.in_(notebook_ids),
                Page.content.isnot(None),
                indexed_filter,
            )
            .order_by(max_sim.desc())
            .limit(limit)
        )
        result = await self.session.execute(statement)
        return [
            PageTrgmHit(page_id=row.id, score=float(row.score), content=row.content)
            for row in result.all()
        ]

    async def get_pages_with_path(self, page_ids: list[int]) -> list[PageWithPath]:
        """Resolve notebook + section path and per-row sync status for SearchHit assembly."""
        if not page_ids:
            return []

        statement = (
            select(
                Page.id,
                Page.title,
                Page.sync_status,
                Section.display_name.label("section_name"),
                Notebook.id.label("notebook_id"),
                Notebook.display_name.label("notebook_name"),
                Notebook.sync_status.label("notebook_sync_status"),
            )
            .join(Section, Page.section_id == Section.id)
            .join(Notebook, Section.notebook_id == Notebook.id)
            .where(Page.id.in_(page_ids))
        )
        result = await self.session.execute(statement)
        return [
            PageWithPath(
                page_id=row.id,
                page_title=row.title,
                section_name=row.section_name,
                notebook_id=row.notebook_id,
                notebook_name=row.notebook_name,
                page_sync_status=row.sync_status,
                notebook_sync_status=row.notebook_sync_status,
            )
            for row in result.all()
        ]

    async def list_by_section(self, section_id: int) -> list[PageResponse]:
        rows = await self.session.scalars(select(Page).where(Page.section_id == section_id))
        return [PageResponse.model_validate(row) for row in rows.all()]

    async def upsert_many(self, section_id: int, data: list[PageCreate]) -> list[PageResponse]:
        values = [{"section_id": section_id, **page.model_dump()} for page in data]
        insert_statement = pg_insert(Page).values(values)
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["section_id", "onenote_id"],
            set_={"title": insert_statement.excluded.title},
        )
        await self.session.execute(upsert_statement)
        onenote_ids = [page.onenote_id for page in data]
        rows = await self.session.scalars(
            select(Page).where(Page.section_id == section_id, Page.onenote_id.in_(onenote_ids))
        )
        return [PageResponse.model_validate(row) for row in rows.all()]

    async def update(self, page_id: int, data: PageUpdate) -> None:
        await self.session.execute(
            update(Page).where(Page.id == page_id).values(**data.model_dump(exclude_unset=True))
        )

    async def delete_many(self, page_ids: list[int]) -> None:
        await self.session.execute(delete(Page).where(Page.id.in_(page_ids)))
