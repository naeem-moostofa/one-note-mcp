"""Server-side store for in-flight Microsoft logins, keyed by OAuth `state`.

Replaces the `oauth_flow` cookie (dropped by browsers during the WorkOS bridge's
cross-site redirect bounce). Rows are short-lived: AuthService deletes one as soon
as it is redeemed and sweeps stale rows by `created_at`. See
plans/mcp-oauth-web-clients.md.
"""

from datetime import datetime

from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OAuthLoginFlow
from app.schemas import OAuthLoginFlowCreate, OAuthLoginFlowResponse


class OAuthLoginFlowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, data: OAuthLoginFlowCreate) -> None:
        await self.session.execute(insert(OAuthLoginFlow).values(**data.model_dump()))

    async def get_by_state(self, state: str) -> OAuthLoginFlowResponse | None:
        row = await self.session.get(OAuthLoginFlow, state)
        return OAuthLoginFlowResponse.model_validate(row) if row else None

    async def delete_by_state(self, state: str) -> None:
        await self.session.execute(delete(OAuthLoginFlow).where(OAuthLoginFlow.state == state))

    async def delete_created_before(self, cutoff: datetime) -> None:
        await self.session.execute(delete(OAuthLoginFlow).where(OAuthLoginFlow.created_at < cutoff))
