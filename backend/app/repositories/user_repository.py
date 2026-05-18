from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.schemas import UserCreate, UserResponse


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: int) -> UserResponse | None:
        row = await self.session.get(User, user_id)
        return UserResponse.model_validate(row) if row else None

    async def upsert(self, data: UserCreate) -> UserResponse:
        insert_statement = pg_insert(User).values(**data.model_dump())
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["microsoft_oid"],
            set_={"email": insert_statement.excluded.email, "display_name": insert_statement.excluded.display_name},
        )
        await self.session.execute(upsert_statement)
        row = await self.session.scalar(select(User).where(User.microsoft_oid == data.microsoft_oid))
        return UserResponse.model_validate(row)
