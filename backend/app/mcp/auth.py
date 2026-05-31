"""
FastMCP-blessed bearer-token authentication for the OneNote MCP server.

Implements a custom `TokenVerifier` that validates raw connection tokens
against our `mcp_connections` table via `MCPConnectionService.resolve_token`
(hash → lookup → revoked-check → scope intersection → `last_used_at` touch).
FastMCP runs the verifier *before* any tool body executes, so unauthenticated
calls become proper 401 responses on the wire rather than tool errors.

The resolved scope is carried back into tool functions through
`AccessToken.claims`, so tools can rehydrate a `ResolvedMCPConnection` via
`current_scope()` without re-running the auth path.
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from app.core.database import AsyncSessionLocal
from app.schemas import ResolvedMCPConnection
from app.services.mcp_connection_service import MCPConnectionService


# Prefixed claim keys so they can't collide with anything FastMCP / future
# providers might stash in the same dict.
_CLAIM_CONNECTION_ID = "onenote_mcp_connection_id"
_CLAIM_ALLOWED_NOTEBOOK_IDS = "onenote_mcp_allowed_notebook_ids"


class MCPConnectionTokenVerifier(TokenVerifier):
    """Validates raw bearer tokens against the local `mcp_connections` table."""

    async def verify_token(self, token: str) -> AccessToken | None:
        # The verifier is constructed once at app startup, so it can't share a
        # session with the eventual tool body — open one per verification.
        async with AsyncSessionLocal() as session:
            try:
                scope = await MCPConnectionService(session).resolve_token(token)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        if scope is None:
            return None  # FastMCP responds 401 to the client

        return AccessToken(
            token=token,
            client_id=str(scope.user_id),
            scopes=[],  # we don't model OAuth-style scopes; allowed notebooks live in claims
            claims={
                _CLAIM_CONNECTION_ID: scope.connection_id,
                _CLAIM_ALLOWED_NOTEBOOK_IDS: scope.allowed_notebook_ids,
            },
        )


def current_scope() -> ResolvedMCPConnection:
    """Return the resolved MCP scope for the current authenticated tool call.

    Reads back the `AccessToken` FastMCP injected after the verifier ran and
    rehydrates the `ResolvedMCPConnection` shape we use everywhere else.
    Raises if called outside an authenticated request (which would only happen
    if a tool is invoked over a transport without auth — not the case here).
    """
    access = get_access_token()
    if access is None:
        raise RuntimeError("current_scope() called outside an authenticated MCP request")
    return ResolvedMCPConnection(
        connection_id=access.claims[_CLAIM_CONNECTION_ID],
        user_id=int(access.client_id),
        allowed_notebook_ids=list(access.claims[_CLAIM_ALLOWED_NOTEBOOK_IDS]),
    )
