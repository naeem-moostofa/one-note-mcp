"""Standalone Connect completion: hand our authenticated user back to AuthKit.

After the bridge runs the existing Microsoft login (which dedupes and returns our
`users` row), it calls WorkOS's completion endpoint with that user. WorkOS upserts
an AuthKit user keyed on our `users.id` as its `external_id` and returns the
`redirect_uri` the browser must follow for AuthKit to mint the OAuth code/token.
WorkOS never sees Microsoft/Graph credentials — only id + email + name. See
plans/mcp-oauth-web-clients.md.
"""

from __future__ import annotations

from functools import lru_cache

from workos import AsyncWorkOSClient
from workos.connect.models import UserObject

from app.core.config import settings
from app.schemas import UserResponse


@lru_cache
def _get_workos_client() -> AsyncWorkOSClient:
    # Reused for the app's lifetime (mirrors get_msal_client); the underlying httpx
    # client is cleaned up on process exit.
    return AsyncWorkOSClient(
        api_key=settings.WORKOS_API_KEY,
        client_id=settings.WORKOS_CLIENT_ID,
    )


class WorkOSBridgeService:
    """Thin wrapper over the Standalone Connect completion call."""

    def __init__(self) -> None:
        self._client = _get_workos_client()

    async def complete_login(self, external_auth_id: str, user: UserResponse) -> str:
        """Complete the external-auth flow for `user`; return the AuthKit redirect URI."""
        response = await self._client.connect.complete_oauth2(
            external_auth_id=external_auth_id,
            user=UserObject(
                id=str(user.id),       # becomes AuthKit external_id; dedup/correlation key
                email=user.email,
                name=user.display_name or None,
            ),
        )
        return response.redirect_uri
