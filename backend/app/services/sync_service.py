import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.graph_client import GraphClient, composite_page
from app.clients.msal_client import MSALClient, get_msal_client
from app.clients.ocr_client import OCRClient, get_ocr_client
from app.core.database import AsyncSessionLocal
from app.core.encryption import decrypt, encrypt
from app.core.exceptions import ConflictError, MSALAuthError
from app.models import MicrosoftConnectionStatus, NotebookSyncStatus, PageSyncStatus
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository
from app.repositories.notebook_repository import NotebookRepository
from app.repositories.page_repository import PageRepository
from app.repositories.section_repository import SectionRepository
from app.schemas import (
    MicrosoftConnectionResponse,
    MicrosoftConnectionUpdate,
    NotebookCreate,
    NotebookResponse,
    NotebookUpdate,
    PageCreate,
    PageResponse,
    PageUpdate,
    SectionCreate,
    SectionResponse,
)

logger = logging.getLogger(__name__)


def _compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class SyncService:
    def __init__(
        self,
        session: AsyncSession,
        graph_client: GraphClient,
        msal_client: MSALClient,
        ocr_client: OCRClient | None = None,
        force: bool = False,
    ) -> None:
        self._graph_client = graph_client
        self._msal_client = msal_client
        self._ocr_client = ocr_client
        self._force = force
        self._connection_repo = MicrosoftConnectionRepository(session)
        self._notebook_repo = NotebookRepository(session)
        self._section_repo = SectionRepository(session)
        self._page_repo = PageRepository(session)

    async def run(self) -> None:
        """Full sync — all notebooks, sections, and pages for all active connections."""
        connections = await self._connection_repo.get_all_active()
        for connection in connections:
            try:
                await self._sync_connection(connection)
            except Exception:
                logger.exception("Failed to sync connection %s", connection.id)

    async def sync_notebooks_only(self) -> None:
        """Fetch the notebook list from Graph and upsert to DB. No sections or pages."""
        connections = await self._connection_repo.get_all_active()
        for connection in connections:
            access_token = await self._acquire_token(connection)
            if access_token is None:
                continue
            graph_notebooks = await self._graph_client.get_notebooks(access_token)
            db_notebooks = await self._notebook_repo.upsert_many(
                connection.user_id,
                [
                    NotebookCreate(onenote_id=graph_notebook.id, display_name=graph_notebook.display_name)
                    for graph_notebook in graph_notebooks
                ],
            )
            logger.info("Synced %d notebooks for user %s", len(db_notebooks), connection.user_id)

    async def sync_single_notebook(self, notebook_id: int) -> None:
        """Sync one notebook's sections + pages (+ OCR) by DB id.

        Marks the notebook SYNCING, does the work, and always finalises it to FRESH
        or FAILED — never raises on a sync failure and never leaves it stuck in
        SYNCING. Setting SYNCING is idempotent, so it's safe whether or not the
        caller pre-marked it (the web guard `NotebookService.start_notebook_sync`
        does; the CLI doesn't). Uses the cron's stale-page detection
        (`_sync_notebook` → `_sync_section`). Shared by the CLI (`--notebook-id`) and
        the web background path; matches how the cron handles a per-notebook failure
        (log + FAILED, no re-raise)."""
        notebook = await self._notebook_repo.get_by_id(notebook_id)
        if notebook is None:
            logger.warning("sync_single_notebook: notebook %s not found", notebook_id)
            return
        sync_started_at = datetime.now(timezone.utc)
        await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.SYNCING))
        try:
            connection = await self._connection_repo.get_by_user_id(notebook.user_id)
            if connection is None:
                raise RuntimeError(f"No Microsoft connection for user {notebook.user_id}")
            access_token = await self._acquire_token(connection)
            if access_token is None:
                raise RuntimeError("Re-auth required — reconnect your Microsoft account")
            await self._sync_notebook(notebook, access_token)
            await self._notebook_repo.update(
                notebook_id,
                NotebookUpdate(sync_status=NotebookSyncStatus.FRESH, last_synced_at=sync_started_at),
            )
            logger.info("Notebook '%s' synced successfully", notebook.display_name)
        except Exception:
            logger.exception("Failed to sync notebook '%s'", notebook.display_name)
            await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.FAILED))

    async def _acquire_token(self, connection: MicrosoftConnectionResponse) -> str | None:
        """Acquire a fresh access token and save the updated cache. Returns None if re-auth needed."""
        try:
            token_result = self._msal_client.acquire_token_silent(
                decrypt(connection.encrypted_msal_token_cache)
            )
        except MSALAuthError:
            logger.warning("Re-auth required for connection %s", connection.id)
            await self._connection_repo.update(
                connection.id,
                MicrosoftConnectionUpdate(status=MicrosoftConnectionStatus.NEEDS_REAUTH),
            )
            return None

        await self._connection_repo.update(
            connection.id,
            MicrosoftConnectionUpdate(encrypted_msal_token_cache=encrypt(token_result.serialized_cache)),
        )
        return token_result.access_token

    async def _discover_notebooks(self, user_id: int, access_token: str) -> list[NotebookResponse]:
        """Names-only Graph discovery: list → upsert → delete-stale. Shared by full sync and refresh."""
        graph_notebooks = await self._graph_client.get_notebooks(access_token)
        graph_notebook_ids = {graph_notebook.id for graph_notebook in graph_notebooks}
        logger.info("Found %d notebooks in Graph", len(graph_notebooks))

        db_notebooks = await self._notebook_repo.upsert_many(
            user_id,
            [
                NotebookCreate(onenote_id=graph_notebook.id, display_name=graph_notebook.display_name)
                for graph_notebook in graph_notebooks
            ],
        )

        all_db_notebooks = await self._notebook_repo.list_by_user(user_id)
        notebooks_to_delete = [notebook.id for notebook in all_db_notebooks if notebook.onenote_id not in graph_notebook_ids]
        if notebooks_to_delete:
            logger.info("Deleting %d notebooks removed from Graph", len(notebooks_to_delete))
            await self._notebook_repo.delete_many(notebooks_to_delete)

        return db_notebooks

    async def refresh_notebook_list(self, user_id: int) -> None:
        """Web-triggered names-only discovery for one user. Raises ConflictError if no usable connection."""
        connection = await self._connection_repo.get_by_user_id(user_id)
        if connection is None or connection.status != MicrosoftConnectionStatus.ACTIVE:
            raise ConflictError("No active Microsoft connection — connect your account first")
        access_token = await self._acquire_token(connection)
        if access_token is None:
            # _acquire_token already flipped the connection to NEEDS_REAUTH
            raise ConflictError("Microsoft session expired — reconnect your account")
        await self._discover_notebooks(connection.user_id, access_token)

    async def _sync_connection(self, connection: MicrosoftConnectionResponse) -> None:
        access_token = await self._acquire_token(connection)
        if access_token is None:
            return

        logger.info("Token acquired for user %s", connection.user_id)

        db_notebooks = await self._discover_notebooks(connection.user_id, access_token)

        enabled_notebooks = [notebook for notebook in db_notebooks if notebook.sync_enabled]
        if not enabled_notebooks:
            logger.info("No sync-enabled notebooks — nothing to do")
            return

        logger.info(
            "Syncing %d notebooks: %s",
            len(enabled_notebooks),
            [notebook.display_name for notebook in enabled_notebooks],
        )
        await self._notebook_repo.update_many(
            [notebook.id for notebook in enabled_notebooks],
            NotebookUpdate(sync_status=NotebookSyncStatus.SYNCING),
        )

        for notebook in enabled_notebooks:
            sync_started_at = datetime.now(timezone.utc)
            try:
                await self._sync_notebook(notebook, access_token)
                await self._notebook_repo.update(
                    notebook.id,
                    NotebookUpdate(sync_status=NotebookSyncStatus.FRESH, last_synced_at=sync_started_at),
                )
                logger.info("Notebook '%s' synced successfully", notebook.display_name)
            except Exception:
                logger.exception("Failed to sync notebook '%s'", notebook.display_name)
                await self._notebook_repo.update(
                    notebook.id,
                    NotebookUpdate(sync_status=NotebookSyncStatus.FAILED),
                )

    async def _sync_notebook(self, notebook: NotebookResponse, access_token: str) -> None:
        logger.info("Syncing notebook '%s' (last synced: %s)", notebook.display_name, notebook.last_synced_at or "never")

        graph_sections = await self._graph_client.get_sections(access_token, notebook.onenote_id)
        graph_section_ids = {graph_section.id for graph_section in graph_sections}
        logger.info("  Found %d sections", len(graph_sections))

        db_sections = await self._section_repo.upsert_many(
            notebook.id,
            [
                SectionCreate(onenote_id=graph_section.id, display_name=graph_section.display_name)
                for graph_section in graph_sections
            ],
        )

        all_db_sections = await self._section_repo.list_by_notebook(notebook.id)
        sections_to_delete = [section.id for section in all_db_sections if section.onenote_id not in graph_section_ids]
        if sections_to_delete:
            logger.info("  Deleting %d sections removed from Graph", len(sections_to_delete))
            await self._section_repo.delete_many(sections_to_delete)

        for section in db_sections:
            await self._sync_section(section, access_token, notebook.last_synced_at)

    async def _sync_section(self, section: SectionResponse, access_token: str, notebook_last_synced_at: datetime | None) -> None:
        existing_db_pages = await self._page_repo.list_by_section(section.id)
        graph_pages = await self._graph_client.get_pages(access_token, section.onenote_id)

        if not graph_pages:
            logger.info("    Section '%s': no pages in Graph", section.display_name)
            pages_to_delete = [page.id for page in existing_db_pages]
            if pages_to_delete:
                await self._page_repo.delete_many(pages_to_delete)
            return

        graph_pages_map = {page.id: page for page in graph_pages}

        db_pages = await self._page_repo.upsert_many(
            section.id,
            [PageCreate(onenote_id=graph_page.id, title=graph_page.title) for graph_page in graph_pages],
        )
        db_pages_map = {page.onenote_id: page for page in db_pages}

        pages_to_delete = [page.id for page in existing_db_pages if page.onenote_id not in graph_pages_map]
        if pages_to_delete:
            logger.info("    Section '%s': deleting %d pages removed from Graph", section.display_name, len(pages_to_delete))
            await self._page_repo.delete_many(pages_to_delete)

        if self._force:
            logger.info("    Force mode — syncing all pages regardless of modification time")
        else:
            logger.info("    Last notebook sync: %s", notebook_last_synced_at or "never")

        to_sync = []
        skipped = []
        for graph_page in graph_pages:
            db_page = db_pages_map.get(graph_page.id)
            if db_page and (
                self._force
                or notebook_last_synced_at is None
                or graph_page.last_modified_datetime > notebook_last_synced_at
                or db_page.sync_status == PageSyncStatus.FAILED
            ):
                to_sync.append((graph_page, db_page))
            else:
                logger.info(
                    "    Skipping '%s' — last modified %s, last sync %s",
                    graph_page.title or graph_page.id,
                    graph_page.last_modified_datetime,
                    notebook_last_synced_at,
                )
                skipped.append(graph_page.title or graph_page.id)

        logger.info("    Section '%s': %d pages — %d to sync, %d unchanged", section.display_name, len(graph_pages), len(to_sync), len(skipped))
        if skipped:
            logger.info("      Skipped (not modified since last sync): %s", skipped)

        for graph_page, db_page in to_sync:
            await self._sync_page_content(db_page, access_token)

    async def _sync_page_content(self, page: PageResponse, access_token: str) -> None:
        logger.info("      Syncing page '%s'", page.title or page.onenote_id)
        try:
            page_content = await self._graph_client.get_page_content_with_ink(access_token, page.onenote_id)

            text_elements = [element for element in page_content.elements if element.kind == "text" and element.text]
            image_elements = [element for element in page_content.elements if element.kind == "image" and element.image_url]

            logger.info(
                "        %d text block(s), %d image(s), handwriting=%s",
                len(text_elements), len(image_elements), page_content.has_handwriting,
            )
            if page_content.has_handwriting and not page_content.ink_strokes:
                logger.warning("        InkML fetch failed — ink will not appear in composite")

            # Fetch all images in parallel
            image_bytes_map: dict[str, bytes] = {}
            if image_elements:
                urls = [element.image_url for element in image_elements]
                results = await asyncio.gather(
                    *[self._graph_client.get_page_image(access_token, url) for url in urls],
                    return_exceptions=True,
                )
                for url, result in zip(urls, results):
                    if isinstance(result, Exception):
                        logger.warning("        Failed to fetch image: %s", result)
                    else:
                        image_bytes_map[url] = result  # type: ignore[assignment]

            # Build composite canvas (images at CSS positions, ink strokes on top) and OCR it.
            # Single Vision call per page — the renderer clamps scale so the canvas fits Vision's cap.
            composite_bytes = composite_page(page_content.elements, image_bytes_map, page_content.ink_strokes)

            ocr_text = ""
            if composite_bytes is not None:
                if self._ocr_client is not None:
                    ocr_text = await asyncio.to_thread(self._ocr_client.run_ocr, composite_bytes)
                    logger.info("        Composite OCR: %d chars", len(ocr_text))
                else:
                    logger.info("        Composite built but OCR client not loaded — skipping")

            # Assemble: typed text in visual order, then composite OCR
            text_parts = [element.text for element in text_elements if element.text]
            if ocr_text:
                text_parts.append(ocr_text)

            content = "\n\n".join(text_parts)
            logger.info("        Final content: %d chars", len(content))

            await self._page_repo.update(
                page.id,
                PageUpdate(
                    content=content,
                    content_hash=_compute_hash(content),
                    sync_status=PageSyncStatus.FRESH,
                ),
            )
        except Exception:
            logger.exception("      Failed to sync content for page '%s'", page.title or page.onenote_id)
            await self._page_repo.update(page.id, PageUpdate(sync_status=PageSyncStatus.FAILED))


async def run_notebook_sync_background(notebook_id: int) -> None:
    """Self-contained background entry point for a web-triggered notebook sync.

    Owns the whole lifecycle so the router doesn't have to: its own HTTP + DB
    session (the request's session is already closed), the MSAL + OCR clients, and
    commit/rollback. Delegates the actual work to `SyncService.sync_single_notebook`,
    which finalises the notebook to FRESH/FAILED. Mirrors `sync/run.py`'s wiring for
    a single notebook. The notebook is expected to already be marked SYNCING by
    `NotebookService.start_notebook_sync` before this is scheduled."""
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
        async with AsyncSessionLocal() as session:
            service = SyncService(
                session=session,
                graph_client=GraphClient(http_client),
                msal_client=get_msal_client(),
                ocr_client=get_ocr_client(),
            )
            try:
                await service.sync_single_notebook(notebook_id)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Background sync crashed for notebook %s", notebook_id)
