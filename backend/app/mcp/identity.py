"""Map a validated WorkOS access token to a resolved MCP scope.

The onmcp_ bearer path resolves scope through `MCPConnectionService.resolve_token`
(a `mcp_connections` row). Web connections have no such row — the WorkOS grant
*is* the connection — so this module resolves scope straight from the token's
subject: look up the user, then expose all of their sync-enabled notebooks.
That mirrors the `scope_all_notebooks` branch of `resolve_token`; web has no
per-notebook picker (see plans/mcp-oauth-web-clients.md).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.notebook_repository import NotebookRepository
from app.repositories.user_repository import UserRepository
from app.schemas import ResolvedMCPConnection

logger = logging.getLogger(__name__)


# Web grants aren't revoked per-row (revocation is WorkOS-side), so there's no
# mcp_connections.id to carry — current_scope() only reads it back as an opaque
# value, so a sentinel is fine.
_WEB_CONNECTION_SENTINEL = 0


def extract_user_id_from_claims(claims: dict) -> int | None:
    """Pull our internal `users.id` out of a WorkOS JWT's claims.

    We pass `users.id` to the completion API as the AuthKit `external_id`, so a
    validated token carries it back as the `external_id` claim (AuthKit's own `sub`
    is an opaque `user_…`). A token with no integer `external_id` means AuthKit
    isn't emitting it — a configuration error the Phase-0 spike must surface — so
    we log loudly and return None (→ 401) rather than guessing other claims.
    """
    value = claims.get("external_id")
    if isinstance(value, (str, int)):
        try:
            return int(value)
        except ValueError:
            pass
    logger.warning(
        "WorkOS JWT carries no integer external_id claim (got %r) — AuthKit must "
        "be configured to emit users.id as external_id; cannot map token to a user.",
        value,
    )
    return None


async def resolve_jwt_identity(user_id: int, session: AsyncSession) -> ResolvedMCPConnection | None:
    """Resolve a WorkOS-authenticated user to their MCP scope.

    Returns None when no `users` row matches (an un-onboarded subject → 401).
    An existing user with no sync-enabled notebooks resolves to an empty
    `allowed_notebook_ids` — authenticated, but tools see nothing until they
    connect and sync in the app. We never auto-create a user here.
    """
    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return None

    notebooks = await NotebookRepository(session).list_by_user(user_id)
    allowed = sorted(notebook.id for notebook in notebooks if notebook.sync_enabled)
    return ResolvedMCPConnection(
        connection_id=_WEB_CONNECTION_SENTINEL,
        user_id=user_id,
        allowed_notebook_ids=allowed,
    )
