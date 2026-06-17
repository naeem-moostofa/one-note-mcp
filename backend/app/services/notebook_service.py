"""Notebook listing and per-notebook settings for the MCP layer and the web routers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ForbiddenError, ResourceNotFoundError
from app.models import (
    MicrosoftConnectionStatus,
    NotebookSyncStatus,
    SyncJobKind,
    SyncJobSource,
)
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository
from app.repositories.notebook_repository import NotebookRepository
from app.repositories.sync_job_repository import SyncJobRepository

# Manual (user-clicked) jobs outrank cron-fanned content jobs (priority 0) so a click isn't
# stuck behind a bulk auto-sync.
_MANUAL_JOB_PRIORITY = 100
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
        self._session = session
        self._notebook_repo = NotebookRepository(session)
        self._connection_repo = MicrosoftConnectionRepository(session)
        self._sync_job_repo = SyncJobRepository(session)

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
        if not enabled:
            # Don't spend the Graph budget on a notebook the user just turned off; a job already
            # running finishes (the worker re-checks sync_enabled and no-ops at claim).
            await self._sync_job_repo.cancel_pending_for_notebook(notebook_id)
        updated = await self._notebook_repo.get_by_id(notebook_id)
        if updated is None:
            raise ResourceNotFoundError("Notebook not found")
        return self._to_web_response(updated)

    async def start_notebook_sync(self, user_id: int, notebook_id: int) -> bool:
        """Guarded entry point for a web-triggered notebook sync — enqueue-only.

        404 if it doesn't exist, 403 if it isn't owned, 409 if sync is disabled or the
        Microsoft connection is missing/expired. Enqueues a high-priority
        `NOTEBOOK_CONTENT` job rather than running anything in the request: the worker
        is the sole Graph executor. Enqueue is idempotent — the partial unique index
        collapses spam-clicks to one active job — so a duplicate click returns False
        (no new job) instead of erroring. The notebook is optimistically marked SYNCING
        so the poll-based dashboard shows progress immediately; the worker then drives
        it to FRESH/FAILED."""
        notebook = await self._notebook_repo.get_by_id(notebook_id)
        if notebook is None:
            raise ResourceNotFoundError("Notebook not found")
        if notebook.user_id != user_id:
            raise ForbiddenError("Not your notebook")
        if not notebook.sync_enabled:
            raise ConflictError("Enable sync before syncing this notebook")

        connection = await self._connection_repo.get_by_user_id(user_id)
        if connection is None or connection.status != MicrosoftConnectionStatus.ACTIVE:
            raise ConflictError("No active Microsoft connection — connect your account first")

        job = await self._sync_job_repo.enqueue(
            kind=SyncJobKind.NOTEBOOK_CONTENT,
            connection_id=connection.id,
            user_id=user_id,
            notebook_id=notebook_id,
            source=SyncJobSource.MANUAL,
            priority=_MANUAL_JOB_PRIORITY,
        )
        await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.SYNCING))
        await self._session.commit()
        return job is not None
