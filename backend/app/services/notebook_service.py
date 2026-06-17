"""Notebook listing and per-notebook settings for the MCP layer and the web routers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, ResourceNotFoundError
from app.models import NotebookSyncStatus
from app.repositories.notebook_repository import NotebookRepository
from app.schemas import (
    NotebookFilter,
    NotebookResponse,
    NotebookSummary,
    NotebookUpdate,
    NotebookWebResponse,
    PaginatedResponse,
)


class NotebookService:
    def __init__(self, session: AsyncSession) -> None:
        self._notebook_repo = NotebookRepository(session)

    @staticmethod
    def _to_web_response(notebook: NotebookResponse) -> NotebookWebResponse:
        return NotebookWebResponse(
            id=notebook.id,
            display_name=notebook.display_name,
            sync_enabled=notebook.sync_enabled,
            sync_status=notebook.sync_status,
            last_synced_at=notebook.last_synced_at,
            last_modified_datetime=notebook.last_modified_datetime,
        )

    async def list_enabled_summaries(
        self,
        user_id: int,
        filter_notebook_ids: list[int] | None = None,
    ) -> list[NotebookSummary]:
        """MCP-scoped: sync-enabled notebooks only, optionally narrowed to filter_notebook_ids."""
        notebooks = await self._notebook_repo.list_by_user(user_id)
        allowed = set(filter_notebook_ids) if filter_notebook_ids is not None else None
        return [
            NotebookSummary(id=notebook.id, display_name=notebook.display_name)
            for notebook in notebooks
            if notebook.sync_enabled and (allowed is None or notebook.id in allowed)
        ]

    async def list_for_user(
        self, user_id: int, filters: NotebookFilter
    ) -> PaginatedResponse[NotebookWebResponse]:
        """Web: one filtered, paginated page of the user's notebooks (enabled and
        disabled) with sync state. Projects the internal rows to the web shape and
        preserves total/limit/offset from the repository."""
        page = await self._notebook_repo.list_page_by_user(user_id, filters)
        return PaginatedResponse(
            data=[self._to_web_response(notebook) for notebook in page.data],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )

    async def set_sync_enabled(self, user_id: int, notebook_id: int, enabled: bool) -> NotebookWebResponse:
        """Flip sync_enabled — 404 if the notebook doesn't exist, 403 if it isn't owned.

        Returns the authoritative updated notebook so clients do not need to
        optimistically rewrite filtered/paginated list pages."""
        notebook = await self._notebook_repo.get_by_id(notebook_id)
        if notebook is None:
            raise ResourceNotFoundError("Notebook not found")
        if notebook.user_id != user_id:
            raise ForbiddenError("Not your notebook")
        await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_enabled=enabled))
        updated = await self._notebook_repo.get_by_id(notebook_id)
        if updated is None:
            raise ResourceNotFoundError("Notebook not found")
        return self._to_web_response(updated)

    async def start_notebook_sync(self, user_id: int, notebook_id: int) -> bool:
        """Guarded entry point for a web-triggered background notebook sync.

        404 if it doesn't exist, 403 if it isn't owned. If a sync is already in
        flight, returns False (don't start a duplicate run). Otherwise marks the
        notebook SYNCING, commits that state so the background task's separate DB
        session does not block behind the request transaction, and returns True
        so the caller launches the background sync."""
        notebook = await self._notebook_repo.get_by_id(notebook_id)
        if notebook is None:
            raise ResourceNotFoundError("Notebook not found")
        if notebook.user_id != user_id:
            raise ForbiddenError("Not your notebook")
        if not notebook.sync_enabled:
            raise ConflictError("Enable sync before syncing this notebook")
        if notebook.sync_status == NotebookSyncStatus.SYNCING:
            return False
        await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.SYNCING))
        await self._notebook_repo.session.commit()
        return True
