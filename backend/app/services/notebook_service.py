"""Notebook listing and per-notebook settings for the MCP layer and the web routers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, ResourceNotFoundError
from app.repositories.notebook_repository import NotebookRepository
from app.schemas import NotebookSummary, NotebookUpdate, NotebookWebResponse


class NotebookService:
    def __init__(self, session: AsyncSession) -> None:
        self._notebook_repo = NotebookRepository(session)

    async def list_enabled_summaries(
        self,
        user_id: int,
        filter_notebook_ids: list[int] | None = None,
    ) -> list[NotebookSummary]:
        """MCP-scoped: sync-enabled notebooks only, optionally narrowed to filter_notebook_ids."""
        notebooks = await self._notebook_repo.list_by_user(user_id)
        allowed = set(filter_notebook_ids) if filter_notebook_ids is not None else None
        return [
            NotebookSummary(id=nb.id, display_name=nb.display_name)
            for nb in notebooks
            if nb.sync_enabled and (allowed is None or nb.id in allowed)
        ]

    async def list_for_user(self, user_id: int) -> list[NotebookWebResponse]:
        """Web: every notebook the user owns (enabled and disabled) with sync state."""
        notebooks = await self._notebook_repo.list_by_user(user_id)
        return [
            NotebookWebResponse(
                id=nb.id,
                display_name=nb.display_name,
                sync_enabled=nb.sync_enabled,
                sync_status=nb.sync_status,
                last_synced_at=nb.last_synced_at,
            )
            for nb in notebooks
        ]

    async def set_sync_enabled(self, user_id: int, notebook_id: int, enabled: bool) -> None:
        """Flip sync_enabled — 404 if the notebook doesn't exist, 403 if it isn't owned.

        Returns nothing: this is a deterministic single-field flip with no
        server-derived side effects, so the caller already knows the resulting
        state (the router answers 204). If toggling ever gains side effects,
        switch to a real refetch and return the authoritative resource."""
        nb = await self._notebook_repo.get_by_id(notebook_id)
        if nb is None:
            raise ResourceNotFoundError("Notebook not found")
        if nb.user_id != user_id:
            raise ForbiddenError("Not your notebook")
        await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_enabled=enabled))
