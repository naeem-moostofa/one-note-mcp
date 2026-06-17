from sqlalchemy import ColumnElement, case, delete, func, nullslast, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notebook, NotebookSyncStatus
from app.schemas import (
    NotebookCreate,
    NotebookFilter,
    NotebookResponse,
    NotebookUpdate,
    PaginatedResponse,
)

# Canonical web ordering: actively syncing notebooks first, then synced notebooks,
# then everything else. Within each bucket, keep the existing most-recently-edited
# ordering, with name + id as deterministic tie-breakers across paginated pages.
_SYNC_PRIORITY = case(
    (Notebook.sync_status == NotebookSyncStatus.SYNCING, 0),
    (
        (Notebook.sync_enabled.is_(True)) & (Notebook.sync_status == NotebookSyncStatus.FRESH),
        1,
    ),
    else_=2,
)
_WEB_ORDER = (
    _SYNC_PRIORITY,
    nullslast(Notebook.last_modified_datetime.desc()),
    func.lower(Notebook.display_name),
    Notebook.id,
)


def _filter_conditions(user_id: int, filters: NotebookFilter) -> list[ColumnElement[bool]]:
    """Shared WHERE predicates so the total count and the row page filter identically."""
    conditions: list[ColumnElement[bool]] = [Notebook.user_id == user_id]
    if filters.search:
        search = filters.search.strip()
        if search:
            conditions.append(Notebook.display_name.ilike(f"%{search}%"))
    if filters.sync_enabled is not None:
        conditions.append(Notebook.sync_enabled == filters.sync_enabled)
    if filters.sync_status is not None:
        conditions.append(Notebook.sync_status == filters.sync_status)
    return conditions


class NotebookRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, notebook_id: int) -> NotebookResponse | None:
        row = await self.session.get(Notebook, notebook_id)
        return NotebookResponse.model_validate(row) if row else None

    async def list_by_user(self, user_id: int) -> list[NotebookResponse]:
        """All of the user's notebooks, unpaginated — for internal callers (sync
        discovery, MCP summaries). The web list uses list_page_by_user instead."""
        rows = await self.session.scalars(
            select(Notebook)
            .where(Notebook.user_id == user_id)
            .order_by(func.lower(Notebook.display_name), Notebook.id)
        )
        return [NotebookResponse.model_validate(row) for row in rows.all()]

    async def list_page_by_user(
        self, user_id: int, filters: NotebookFilter
    ) -> PaginatedResponse[NotebookResponse]:
        """One filtered, ordered page of the user's notebooks plus the total match count
        (counted before limit/offset so the UI can show "X of N" / drive Load more)."""
        conditions = _filter_conditions(user_id, filters)

        total = await self.session.scalar(
            select(func.count()).select_from(Notebook).where(*conditions)
        )

        rows = await self.session.scalars(
            select(Notebook)
            .where(*conditions)
            .order_by(*_WEB_ORDER)
            .limit(filters.limit)
            .offset(filters.offset)
        )
        return PaginatedResponse(
            data=[NotebookResponse.model_validate(row) for row in rows.all()],
            total=total or 0,
            limit=filters.limit,
            offset=filters.offset,
        )

    async def upsert_many(self, user_id: int, data: list[NotebookCreate]) -> list[NotebookResponse]:
        values = [{"user_id": user_id, **notebook.model_dump()} for notebook in data]
        insert_statement = pg_insert(Notebook).values(values)
        upsert_statement = insert_statement.on_conflict_do_update(
            index_elements=["user_id", "onenote_id"],
            # last_modified_datetime is deliberately NOT updated here — Refresh only
            # touches names. It's computed from the newest page during content Sync
            # (see SyncService._sync_notebook); clobbering it on Refresh would wipe
            # that accurate value back to NULL.
            set_={"display_name": insert_statement.excluded.display_name},
        )
        await self.session.execute(upsert_statement)
        onenote_ids = [notebook.onenote_id for notebook in data]
        rows = await self.session.scalars(
            select(Notebook)
            .where(Notebook.user_id == user_id, Notebook.onenote_id.in_(onenote_ids))
            .order_by(func.lower(Notebook.display_name), Notebook.id)
        )
        return [NotebookResponse.model_validate(row) for row in rows.all()]

    async def update(self, notebook_id: int, data: NotebookUpdate) -> None:
        await self.session.execute(
            update(Notebook)
            .where(Notebook.id == notebook_id)
            .values(**data.model_dump(exclude_unset=True))
        )

    async def update_many(self, notebook_ids: list[int], data: NotebookUpdate) -> None:
        await self.session.execute(
            update(Notebook)
            .where(Notebook.id.in_(notebook_ids))
            .values(**data.model_dump(exclude_unset=True))
        )

    async def delete_many(self, notebook_ids: list[int]) -> None:
        await self.session.execute(delete(Notebook).where(Notebook.id.in_(notebook_ids)))
