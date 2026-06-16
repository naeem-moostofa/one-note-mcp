from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends

from app.core.auth import get_current_user_id
from app.routers.deps import get_notebook_service, get_sync_service
from app.schemas import (
    NotebookFilter,
    NotebookSyncToggleRequest,
    NotebookWebResponse,
    PaginatedResponse,
)
from app.services.notebook_service import NotebookService
from app.services.sync_service import SyncService, run_notebook_sync_background

router = APIRouter(prefix="/api/notebooks", tags=["notebooks"])


@router.get("")
async def list_notebooks(
    filters: Annotated[NotebookFilter, Depends()],
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[NotebookService, Depends(get_notebook_service)],
) -> PaginatedResponse[NotebookWebResponse]:
    return await service.list_for_user(user_id, filters)


@router.patch("/{notebook_id}")
async def toggle_notebook(
    notebook_id: int,
    body: NotebookSyncToggleRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[NotebookService, Depends(get_notebook_service)],
) -> NotebookWebResponse:
    return await service.set_sync_enabled(user_id, notebook_id, body.sync_enabled)


@router.post("/refresh", status_code=204)
async def refresh_notebooks(
    user_id: Annotated[int, Depends(get_current_user_id)],
    sync_service: Annotated[SyncService, Depends(get_sync_service)],
) -> None:
    """Names-only: refresh the *list* of available notebooks from OneNote. Does NOT
    sync page content — that's per-notebook via POST /{id}/sync.

    Returns 204 — the client re-fetches GET /api/notebooks afterwards (it can't return
    "the list" now that the list is paginated and filtered client-side)."""
    await sync_service.refresh_notebook_list(user_id)


@router.post("/{notebook_id}/sync", status_code=202)
async def sync_notebook(
    notebook_id: int,
    background_tasks: BackgroundTasks,
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[NotebookService, Depends(get_notebook_service)],
) -> None:
    """Start a background notebook sync (sections + pages + OCR) for one notebook.

    Returns 202 with no body — the notebook is marked SYNCING and the client polls
    GET /api/notebooks to watch it reach FRESH/FAILED. If the notebook is already
    syncing this is a no-op (no duplicate run)."""
    started = await service.start_notebook_sync(user_id, notebook_id)  # 404/403 + SYNCING guard
    if started:
        background_tasks.add_task(run_notebook_sync_background, notebook_id)
