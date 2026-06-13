"""Assembles the web Account page payload — profile + connection status, never the MSAL cache."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ResourceNotFoundError
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas import MeResponse


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._user_repo = UserRepository(session)
        self._microsoft_connection_repo = MicrosoftConnectionRepository(session)

    async def get_profile(self, user_id: int) -> MeResponse:
        user = await self._user_repo.get_by_id(user_id)
        if user is None:
            raise ResourceNotFoundError("User not found")
        connection = await self._microsoft_connection_repo.get_by_user_id(user_id)
        return MeResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            created_at=user.created_at,
            microsoft_status=connection.status if connection else None,
        )
