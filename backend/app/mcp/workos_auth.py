"""WorkOS AuthKit resource-server auth for the MCP server.

claude.ai / ChatGPT (web) only speak OAuth 2.1, so the MCP server is fronted by
WorkOS AuthKit as the Authorization Server. FastMCP's `AuthKitProvider` makes us
a *resource server*: it serves the Protected Resource Metadata + 401 challenge,
forwards AuthKit's AS metadata, and validates the WorkOS-issued JWT via JWKS —
binding the accepted audience to the resource URL we advertise (no token-reuse
CVE; this is the RemoteAuthProvider path, never the OAuth-Proxy `WorkOSProvider`).

`OneNoteAuthKitProvider` extends it only to translate a validated JWT into the
same `AccessToken.claims` shape the onmcp_ path produces, so `current_scope()`
and the tools stay identical regardless of which token type authenticated.

`build_mcp_auth()` then composes this with the existing `MCPConnectionTokenVerifier`
through FastMCP's `MultiAuth`, so CLI clients (onmcp_ bearer) and web clients
(WorkOS JWT) both work against one server. When WorkOS isn't configured
(local dev), it returns the bare onmcp_ verifier — the web path simply isn't
mounted. See plans/mcp-oauth-web-clients.md.
"""

from __future__ import annotations

from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth
from fastmcp.server.auth.providers.workos import AuthKitProvider

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.mcp.auth import (
    _CLAIM_ALLOWED_NOTEBOOK_IDS,
    _CLAIM_CONNECTION_ID,
    MCPConnectionTokenVerifier,
)
from app.mcp.identity import extract_user_id_from_claims, resolve_jwt_identity


class OneNoteAuthKitProvider(AuthKitProvider):
    """AuthKit JWT validation + translation to our internal scope claims.

    `super().verify_token` runs the JWKS/issuer/audience validation (audience is
    auto-bound to our advertised resource URL). On success we resolve the token's
    subject to a user and their sync-enabled notebooks, then re-emit an
    `AccessToken` carrying the same claim keys the onmcp_ verifier uses. Returning
    None (invalid JWT, or a subject with no `users` row) lets `MultiAuth` fall
    through to the onmcp_ verifier and ultimately 401.
    """

    async def verify_token(self, token: str) -> AccessToken | None:
        validated = await super().verify_token(token)
        if validated is None:
            return None  # not a valid WorkOS JWT — let MultiAuth try the next source

        user_id = extract_user_id_from_claims(validated.claims)
        if user_id is None:
            return None

        async with AsyncSessionLocal() as session:
            try:
                scope = await resolve_jwt_identity(user_id, session)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        if scope is None:
            return None  # authenticated subject but no onboarded user → 401

        return AccessToken(
            token=token,
            client_id=str(scope.user_id),
            scopes=[],  # notebook scope lives in claims, not OAuth scopes
            claims={
                _CLAIM_CONNECTION_ID: scope.connection_id,
                _CLAIM_ALLOWED_NOTEBOOK_IDS: scope.allowed_notebook_ids,
            },
        )


def build_mcp_auth() -> AuthProvider:
    """Compose the MCP auth provider.

    With WorkOS configured: WorkOS AuthKit (web/OAuth) + onmcp_ bearer (CLI),
    tried in that order. Without it: the onmcp_ verifier alone, so local dev and
    the CLI path run with no WorkOS account.
    """
    onmcp_verifier = MCPConnectionTokenVerifier()
    if not settings.WORKOS_AUTHKIT_DOMAIN:
        return onmcp_verifier

    # The dashboard surfaces the domain bare (e.g. "env.authkit.app"); AuthKitProvider
    # needs a full URL for the issuer/JWKS, so default the scheme to https.
    authkit_domain = settings.WORKOS_AUTHKIT_DOMAIN
    if not authkit_domain.startswith(("http://", "https://")):
        authkit_domain = f"https://{authkit_domain}"

    # base_url = the public URL clients connect to; AuthKitProvider derives the
    # advertised Protected Resource Metadata + the JWT audience from it. We reuse
    # MCP_SERVER_URL so the audience equals the exact URL clients are pointed at.
    # [VERIFY in Phase-0 spike] that the PRM is fetchable where claude.ai probes
    # (root `/.well-known/oauth-protected-resource` and any /mcp-suffixed variant)
    # given the outer Starlette mount at /mcp — a misplaced PRM 404s the web flow.
    authkit = OneNoteAuthKitProvider(
        authkit_domain=authkit_domain,
        base_url=settings.MCP_SERVER_URL,
        resource_name="OneNote MCP",
    )
    return MultiAuth(server=authkit, verifiers=[onmcp_verifier])
