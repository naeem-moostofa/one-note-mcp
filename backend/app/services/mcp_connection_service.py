"""
MCP connection lifecycle: issuing tokens, resolving them on incoming requests,
listing for the owning user, and revoking.

The repository (`MCPConnectionRepository`) deliberately knows nothing about
tokens — only hashes. This service owns the raw-token side: generation,
SHA-256 hashing, and the scope resolution that turns a stored
`(scope_all_notebooks, notebook_ids)` pair into a concrete, currently-allowed
notebook ID list.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.mcp_connection_repository import MCPConnectionRepository
from app.repositories.notebook_repository import NotebookRepository
from app.schemas import (
    MCPConnectionCreate,
    MCPConnectionCreatedResponse,
    MCPConnectionResponse,
    MCPConnectionUpdate,
    ResolvedMCPConnection,
)


# Prefix makes raw tokens easy to recognise in logs / vaults — the rest is
# secrets.token_urlsafe(32) which gives ~256 bits of entropy.
_RAW_TOKEN_PREFIX = "onmcp_"


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


class MCPConnectionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = MCPConnectionRepository(session)
        self._notebook_repo = NotebookRepository(session)

    async def resolve_token(self, raw_token: str) -> ResolvedMCPConnection | None:
        """Hash, look up, check revoked, intersect scope with sync-enabled notebooks,
        and touch `last_used_at`. Returns None for any auth failure so the caller
        can map every failure mode to a single 401 without leaking details."""
        connection = await self._repo.get_by_token_hash(_hash_token(raw_token))
        if connection is None or connection.revoked_at is not None:
            return None

        user_notebooks = await self._notebook_repo.list_by_user(connection.user_id)
        sync_enabled_ids = {nb.id for nb in user_notebooks if nb.sync_enabled}
        if connection.scope_all_notebooks:
            allowed = sorted(sync_enabled_ids)
        else:
            requested = set(connection.notebook_ids or [])
            allowed = sorted(requested & sync_enabled_ids)

        await self._repo.update(
            connection.id,
            MCPConnectionUpdate(last_used_at=datetime.now(timezone.utc)),
        )

        return ResolvedMCPConnection(
            connection_id=connection.id,
            user_id=connection.user_id,
            allowed_notebook_ids=allowed,
        )

    async def create(
        self,
        user_id: int,
        scope_all_notebooks: bool,
        notebook_ids: list[int] | None = None,
        display_name: str | None = None,
    ) -> MCPConnectionCreatedResponse:
        """Mint a new connection. The returned `raw_token` is the only chance
        the caller has to see it — only the hash is persisted."""
        raw_token = _RAW_TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = await self._repo.create(
            user_id,
            MCPConnectionCreate(
                token_hash=_hash_token(raw_token),
                display_name=display_name,
                scope_all_notebooks=scope_all_notebooks,
                notebook_ids=notebook_ids if not scope_all_notebooks else None,
            ),
        )
        return MCPConnectionCreatedResponse(
            id=record.id,
            display_name=record.display_name,
            scope_all_notebooks=record.scope_all_notebooks,
            notebook_ids=record.notebook_ids,
            created_at=record.created_at,
            raw_token=raw_token,
        )

    async def list_for_user(self, user_id: int) -> list[MCPConnectionResponse]:
        return await self._repo.list_by_user(user_id)

    async def revoke(self, user_id: int, connection_id: int) -> None:
        """Sets `revoked_at` if the connection belongs to `user_id`. Silent no-op
        otherwise — the REST layer should 404 in that case."""
        owned = await self._repo.list_by_user(user_id)
        if not any(c.id == connection_id for c in owned):
            return
        await self._repo.update(
            connection_id,
            MCPConnectionUpdate(revoked_at=datetime.now(timezone.utc)),
        )
