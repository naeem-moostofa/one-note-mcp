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

from app.core.config import settings
from app.core.exceptions import ForbiddenError, InvalidRequestError, ResourceNotFoundError
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
        sync_enabled_ids = {notebook.id for notebook in user_notebooks if notebook.sync_enabled}
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
        """Mint a connection. raw_token is returned once; only its hash is persisted.
        A scoped connection must name notebooks the caller owns."""
        if not scope_all_notebooks:
            if not notebook_ids:
                raise InvalidRequestError("notebook_ids required when scope_all_notebooks is False")
            owned = {notebook.id for notebook in await self._notebook_repo.list_by_user(user_id)}
            if not set(notebook_ids).issubset(owned):
                # Non-leaking: doesn't reveal which id is unowned.
                raise InvalidRequestError("one or more notebook_ids are invalid")

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
            mcp_url=settings.MCP_SERVER_URL,
        )

    async def list_for_user(self, user_id: int) -> list[MCPConnectionResponse]:
        return await self._repo.list_by_user(user_id)

    async def revoke(self, user_id: int, connection_id: int) -> None:
        """Set revoked_at — 404 if the connection doesn't exist, 403 if it isn't owned."""
        connection = await self._repo.get_by_id(connection_id)
        if connection is None:
            raise ResourceNotFoundError("Connection not found")
        if connection.user_id != user_id:
            raise ForbiddenError("Not your connection")
        await self._repo.update(
            connection_id,
            MCPConnectionUpdate(revoked_at=datetime.now(timezone.utc)),
        )
