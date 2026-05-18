from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MCPConnection
from app.schemas import MCPConnectionCreate, MCPConnectionResponse, MCPConnectionUpdate


class MCPConnectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_token_hash(self, token_hash: str) -> MCPConnectionResponse | None:
        row = await self.session.scalar(
            select(MCPConnection).where(MCPConnection.token_hash == token_hash)
        )
        return MCPConnectionResponse.model_validate(row) if row else None

    async def list_by_user(self, user_id: int) -> list[MCPConnectionResponse]:
        rows = await self.session.scalars(select(MCPConnection).where(MCPConnection.user_id == user_id))
        return [MCPConnectionResponse.model_validate(row) for row in rows.all()]

    async def create(self, user_id: int, data: MCPConnectionCreate) -> MCPConnectionResponse:
        connection = MCPConnection(user_id=user_id, **data.model_dump())
        self.session.add(connection)
        await self.session.flush()
        await self.session.refresh(connection)
        return MCPConnectionResponse.model_validate(connection)

    async def update(self, connection_id: int, data: MCPConnectionUpdate) -> None:
        await self.session.execute(
            update(MCPConnection)
            .where(MCPConnection.id == connection_id)
            .values(**data.model_dump(exclude_unset=True))
        )
