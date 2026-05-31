"""
FastMCP dependencies for the OneNote MCP tools.

Right now there's just one — a per-request DB session with commit-on-success /
rollback-on-error semantics. Tools declare it via `session: AsyncSession =
Depends(get_db_session)`; FastMCP resolves it before the tool body runs and
runs the teardown (commit or rollback) after the body returns or raises.

Caching is per-request, so if we add a second dep that also wants a session
(or `current_scope` ever takes one), they share the same session/transaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal


@asynccontextmanager
async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
