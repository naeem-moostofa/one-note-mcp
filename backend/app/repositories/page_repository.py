from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook, Page, Section
from app.schemas import PageCreate, PageDetailResponse, PageResponse, PageSearchQuery, PageSearchResponse, PageUpdate, PaginatedResponse


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
        result = await self.session.execute(
            select(
                Page.id,
                Page.onenote_id,
                Page.title,
                Page.content,
                Page.content_hash,
                Page.sync_status,
                Page.last_synced_at,
                Section.display_name.label("section_name"),
                Notebook.display_name.label("notebook_name"),
            )
            .join(Section, Page.section_id == Section.id)
            .join(Notebook, Section.notebook_id == Notebook.id)
            .where(Page.id == page_id)
        )
        row = result.one_or_none()
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
