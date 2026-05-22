import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.msal_client import MSALClient
from app.core.auth import create_jwt
from app.core.encryption import encrypt
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository
from app.repositories.user_repository import UserRepository
from app.schemas import MicrosoftConnectionCreate, MSALAuthCodeFlow, UserCreate


class AuthService:
    def __init__(self, session: AsyncSession, msal_client: MSALClient) -> None:
        self._msal_client = msal_client
        self._user_repo = UserRepository(session)
        self._connection_repo = MicrosoftConnectionRepository(session)

    def begin_login(self) -> MSALAuthCodeFlow:
        """Build the MSAL auth code flow. Caller is responsible for persisting it as a cookie."""
        state = secrets.token_urlsafe(32)
        return self._msal_client.get_auth_code_flow(state)

    async def complete_login(self, flow: MSALAuthCodeFlow, auth_response: dict) -> str:
        """Exchange the auth code for tokens and upsert the user. Returns a signed JWT."""
        token_result = self._msal_client.exchange_code(flow, auth_response)
        claims = token_result.id_token_claims

        user = await self._user_repo.upsert(UserCreate(
            microsoft_oid=claims.oid,
            email=claims.email or claims.preferred_username or "",
            display_name=claims.name or "",
        ))

        await self._connection_repo.upsert(user.id, MicrosoftConnectionCreate(
            encrypted_msal_token_cache=encrypt(token_result.serialized_cache),
        ))

        return create_jwt(user.id)

    async def disconnect(self, user_id: int) -> None:
        await self._connection_repo.delete_by_user_id(user_id)
