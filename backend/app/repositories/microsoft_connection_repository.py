from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MicrosoftConnection, MicrosoftConnectionStatus
from app.schemas import MicrosoftConnectionCreate, MicrosoftConnectionResponse, MicrosoftConnectionUpdate


class MicrosoftConnectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_user_id(self, user_id: int) -> MicrosoftConnectionResponse | None:
        row = await self.session.scalar(
            select(MicrosoftConnection).where(MicrosoftConnection.user_id == user_id)
        )
        return MicrosoftConnectionResponse.model_validate(row) if row else None

    async def get_all_active(self) -> list[MicrosoftConnectionResponse]:
        rows = await self.session.scalars(
            select(MicrosoftConnection).where(MicrosoftConnection.status == MicrosoftConnectionStatus.ACTIVE)
        )
        return [MicrosoftConnectionResponse.model_validate(row) for row in rows.all()]

    async def upsert(self, user_id: int, data: MicrosoftConnectionCreate) -> MicrosoftConnectionResponse:
        insert_statement = pg_insert(MicrosoftConnection).values(
            user_id=user_id, status=MicrosoftConnectionStatus.ACTIVE, **data.model_dump()
        )
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["user_id"],
            set_={**data.model_dump(), "status": MicrosoftConnectionStatus.ACTIVE},
        )
        await self.session.execute(upsert_statement)
        row = await self.session.scalar(
            select(MicrosoftConnection).where(MicrosoftConnection.user_id == user_id)
        )
        return MicrosoftConnectionResponse.model_validate(row)

    async def update(self, connection_id: int, data: MicrosoftConnectionUpdate) -> None:
        await self.session.execute(
            update(MicrosoftConnection)
            .where(MicrosoftConnection.id == connection_id)
            .values(**data.model_dump(exclude_unset=True))
        )

    async def delete_by_user_id(self, user_id: int) -> None:
        await self.session.execute(
            delete(MicrosoftConnection).where(MicrosoftConnection.user_id == user_id)
        )
