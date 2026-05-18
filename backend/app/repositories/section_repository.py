from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Section
from app.schemas import SectionCreate, SectionResponse


class SectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_notebook(self, notebook_id: int) -> list[SectionResponse]:
        rows = await self.session.scalars(select(Section).where(Section.notebook_id == notebook_id))
        return [SectionResponse.model_validate(row) for row in rows.all()]

    async def upsert_many(self, notebook_id: int, data: list[SectionCreate]) -> list[SectionResponse]:
        values = [{"notebook_id": notebook_id, **section.model_dump()} for section in data]
        insert_statement = pg_insert(Section).values(values)
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["notebook_id", "onenote_id"],
            set_={"display_name": insert_statement.excluded.display_name},
        )
        await self.session.execute(upsert_statement)
        onenote_ids = [section.onenote_id for section in data]
        rows = await self.session.scalars(
            select(Section).where(Section.notebook_id == notebook_id, Section.onenote_id.in_(onenote_ids))
        )
        return [SectionResponse.model_validate(row) for row in rows.all()]

    async def delete_many(self, section_ids: list[int]) -> None:
        await self.session.execute(delete(Section).where(Section.id.in_(section_ids)))
