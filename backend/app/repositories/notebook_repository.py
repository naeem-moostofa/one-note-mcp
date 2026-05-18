from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook
from app.schemas import NotebookCreate, NotebookResponse, NotebookUpdate


class NotebookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, notebook_id: int) -> NotebookResponse | None:
        row = await self.session.get(Notebook, notebook_id)
        return NotebookResponse.model_validate(row) if row else None

    async def list_by_user(self, user_id: int) -> list[NotebookResponse]:
        rows = await self.session.scalars(select(Notebook).where(Notebook.user_id == user_id))
        return [NotebookResponse.model_validate(row) for row in rows.all()]

    async def upsert_many(self, user_id: int, data: list[NotebookCreate]) -> list[NotebookResponse]:
        values = [{"user_id": user_id, **notebook.model_dump()} for notebook in data]
        insert_statement = pg_insert(Notebook).values(values)
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["user_id", "onenote_id"],
            set_={"display_name": insert_statement.excluded.display_name},
        )
        await self.session.execute(upsert_statement)
        onenote_ids = [notebook.onenote_id for notebook in data]
        rows = await self.session.scalars(
            select(Notebook).where(Notebook.user_id == user_id, Notebook.onenote_id.in_(onenote_ids))
        )
        return [NotebookResponse.model_validate(row) for row in rows.all()]

    async def update(self, notebook_id: int, data: NotebookUpdate) -> None:
        await self.session.execute(
            update(Notebook)
            .where(Notebook.id == notebook_id)
            .values(**data.model_dump(exclude_unset=True))
        )

    async def update_many(self, notebook_ids: list[int], data: NotebookUpdate) -> None:
        await self.session.execute(
            update(Notebook)
            .where(Notebook.id.in_(notebook_ids))
            .values(**data.model_dump(exclude_unset=True))
        )

    async def delete_many(self, notebook_ids: list[int]) -> None:
        await self.session.execute(delete(Notebook).where(Notebook.id.in_(notebook_ids)))
