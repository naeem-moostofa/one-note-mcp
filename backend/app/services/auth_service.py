import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.msal_client import MSALClient
from app.core.auth import create_jwt
from app.core.encryption import decrypt, encrypt
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository
from app.repositories.oauth_login_flow_repository import OAuthLoginFlowRepository
from app.repositories.user_repository import UserRepository
from app.schemas import (
    MicrosoftConnectionCreate,
    MSALAuthCodeFlow,
    OAuthLoginFlowCreate,
    UserCreate,
    UserResponse,
)

# How long an in-flight login stays valid between redirect and callback — long
# enough for any login interaction, short enough to bound replay of a stale state.
_LOGIN_FLOW_TTL = timedelta(minutes=10)


class AuthService:
    def __init__(self, session: AsyncSession, msal_client: MSALClient) -> None:
        self._msal_client = msal_client
        self._user_repo = UserRepository(session)
        self._connection_repo = MicrosoftConnectionRepository(session)
        self._login_flow_repo = OAuthLoginFlowRepository(session)

    def begin_login(self) -> MSALAuthCodeFlow:
        """Build the MSAL auth code flow. Caller must persist it via persist_flow."""
        state = secrets.token_urlsafe(32)
        return self._msal_client.get_auth_code_flow(state)

    async def persist_flow(self, flow: MSALAuthCodeFlow, external_auth_id: str | None = None) -> None:
        """Stash an in-flight login server-side, keyed by its OAuth `state` (set
        `external_auth_id` for WorkOS bridge logins). The callback recovers it via
        pop_flow — no cookie, so it survives the bridge's cross-site redirect."""
        await self._login_flow_repo.delete_created_before(datetime.now(timezone.utc) - _LOGIN_FLOW_TTL)
        await self._login_flow_repo.create(OAuthLoginFlowCreate(
            state=flow.state,
            encrypted_flow=encrypt(flow.model_dump_json()),
            external_auth_id=external_auth_id,
        ))

    async def pop_flow(self, state: str) -> tuple[MSALAuthCodeFlow, str | None] | None:
        """Recover and consume the flow for `state`. Returns (flow, external_auth_id)
        or None if unknown/expired. external_auth_id is None for normal SPA logins."""
        stored = await self._login_flow_repo.get_by_state(state)
        if stored is None:
            return None
        await self._login_flow_repo.delete_by_state(state)  # single-use, even if expired
        if datetime.now(timezone.utc) - stored.created_at > _LOGIN_FLOW_TTL:
            return None
        flow = MSALAuthCodeFlow.model_validate_json(decrypt(stored.encrypted_flow))
        return flow, stored.external_auth_id

    async def complete_login(self, flow: MSALAuthCodeFlow, auth_response: dict) -> str:
        """Exchange the auth code for tokens and upsert the user. Returns a signed JWT."""
        user = await self.complete_login_user(flow, auth_response)
        return create_jwt(user.id)

    async def complete_login_user(self, flow: MSALAuthCodeFlow, auth_response: dict) -> UserResponse:
        """Exchange the auth code, upsert the (oid-deduped) user + MSAL cache, and
        return the user. The OAuth bridge needs the user row itself, not a JWT."""
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

        return user

    async def disconnect(self, user_id: int) -> None:
        await self._connection_repo.delete_by_user_id(user_id)
