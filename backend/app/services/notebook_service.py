"""
Notebook listing for the MCP layer (and future REST routers).

Thin wrapper over `NotebookRepository`: project to the lean `NotebookSummary`
shape, default to only sync-enabled notebooks, optionally narrow to a caller-
supplied ID set. Deliberately scope-blind — callers (MCP tool, REST router,
future jobs) pass in whatever filter they need; the service doesn't know
about `ResolvedMCPConnection`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.notebook_repository import NotebookRepository
from app.schemas import NotebookSummary


class NotebookService:
    def __init__(self, session: AsyncSession) -> None:
        self._notebook_repo = NotebookRepository(session)

    async def list_for_user(
        self,
        user_id: int,
        filter_notebook_ids: list[int] | None = None,
    ) -> list[NotebookSummary]:
        """List a user's sync-enabled notebooks, projected to `NotebookSummary`.

        If `filter_notebook_ids` is provided, only notebooks whose id is in
        that set are returned. Pass it as `None` to get every sync-enabled
        notebook the user owns.
        """
        notebooks = await self._notebook_repo.list_by_user(user_id)
        allowed = set(filter_notebook_ids) if filter_notebook_ids is not None else None
        return [
            NotebookSummary(id=nb.id, display_name=nb.display_name)
            for nb in notebooks
            if nb.sync_enabled and (allowed is None or nb.id in allowed)
        ]
