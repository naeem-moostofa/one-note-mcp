import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.graph_client import GraphClient, composite_page
from app.clients.msal_client import MSALClient
from app.clients.ocr_client import OCRClient
from app.core.config import settings
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
    PageContentSyncCandidate,
    PageContentSyncResult,
    PageResponse,
    PageUpdate,
    GraphPage,
    GraphPageElement,
    SectionCreate,
    SectionPages,
    SectionResponse,
    SectionSyncPlan,
)

logger = logging.getLogger(__name__)


def _compute_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _finalize_fresh_update(
    sync_started_at: datetime, latest_page_modified: datetime | None
) -> NotebookUpdate:
    """Build the NotebookUpdate that finalises a notebook FRESH after a content sync.

    Writes last_modified_datetime (the newest page edit time) only when the sync
    actually saw pages — a pageless notebook leaves the existing value untouched
    rather than clobbering it to NULL (the field is excluded when unset)."""
    if latest_page_modified is None:
        return NotebookUpdate(sync_status=NotebookSyncStatus.FRESH, last_synced_at=sync_started_at)
    return NotebookUpdate(
        sync_status=NotebookSyncStatus.FRESH,
        last_synced_at=sync_started_at,
        last_modified_datetime=latest_page_modified,
    )


class SyncService:
    def __init__(
        self,
        session: AsyncSession,
        graph_client: GraphClient,
        msal_client: MSALClient,
        ocr_client: OCRClient | None = None,
        force: bool = False,
        page_worker_concurrency: int = settings.SYNC_PAGE_WORKER_CONCURRENCY,
    ) -> None:
        self._session = session
        self._graph_client = graph_client
        self._msal_client = msal_client
        self._ocr_client = ocr_client
        self._force = force
        # Graph and Vision concurrency are capped inside their clients; this only bounds how
        # many pages are in flight through the pipeline (memory + Pillow CPU).
        self._page_worker_concurrency = max(1, page_worker_concurrency)
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

    async def sync_notebook_content(self, notebook_id: int) -> datetime | None:
        """Core content sync for one notebook (token → sections → pages → OCR).

        Returns the newest page lastModifiedDateTime (None if the notebook has no
        pages), which the caller writes when finalising the notebook FRESH. Raises
        on failure and does **not** touch the notebook's `sync_status` — the caller
        owns that transition: the worker drives it for job-level retry/backoff, and
        `sync_single_notebook` wraps this for the inline CLI path. Setting status
        here would conflict with both."""
        notebook = await self._notebook_repo.get_by_id(notebook_id)
        if notebook is None:
            logger.warning("sync_notebook_content: notebook %s not found", notebook_id)
            return None
        connection = await self._connection_repo.get_by_user_id(notebook.user_id)
        if connection is None:
            raise RuntimeError(f"No Microsoft connection for user {notebook.user_id}")
        access_token = await self._acquire_token(connection)
        if access_token is None:
            raise RuntimeError("Re-auth required — reconnect your Microsoft account")
        return await self._sync_notebook(notebook, access_token)

    async def discover_notebooks(self, connection_id: int) -> list[NotebookResponse]:
        """Names-only discovery for one connection: list → upsert → delete-stale.

        Worker entry point for a `discovery` job. Raises if the connection is gone or
        needs re-auth so the job records a clear error instead of silently no-opping."""
        connection = await self._connection_repo.get_by_id(connection_id)
        if connection is None:
            raise RuntimeError(f"Microsoft connection {connection_id} not found")
        if connection.status != MicrosoftConnectionStatus.ACTIVE:
            raise RuntimeError("Microsoft connection needs re-auth")
        access_token = await self._acquire_token(connection)
        if access_token is None:
            raise RuntimeError("Re-auth required — reconnect your Microsoft account")
        return await self._discover_notebooks(connection.user_id, access_token)

    async def sync_single_notebook(self, notebook_id: int) -> None:
        """Inline single-notebook sync by DB id — self-contained status management.

        Marks the notebook SYNCING, does the work via `sync_notebook_content`, and
        always finalises it to FRESH or FAILED — never raises on a sync failure and
        never leaves it stuck in SYNCING. Used by the CLI debug path
        (`python -m sync.run --notebook-id … --run-inline`). The durable web/cron
        path instead goes through the queue, where the worker owns the same
        transitions plus job-level retry."""
        notebook = await self._notebook_repo.get_by_id(notebook_id)
        if notebook is None:
            logger.warning("sync_single_notebook: notebook %s not found", notebook_id)
            return
        sync_started_at = datetime.now(timezone.utc)
        await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.SYNCING))
        await self._session.commit()
        try:
            latest_page_modified = await self.sync_notebook_content(notebook_id)
            await self._notebook_repo.update(
                notebook_id,
                _finalize_fresh_update(sync_started_at, latest_page_modified),
            )
            await self._session.commit()
            logger.info("Notebook '%s' synced successfully", notebook.display_name)
        except Exception:
            logger.exception("Failed to sync notebook '%s'", notebook.display_name)
            await self._notebook_repo.update(notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.FAILED))
            await self._session.commit()

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
        await self._session.commit()

        for notebook in enabled_notebooks:
            sync_started_at = datetime.now(timezone.utc)
            try:
                latest_page_modified = await self._sync_notebook(notebook, access_token)
                await self._notebook_repo.update(
                    notebook.id,
                    _finalize_fresh_update(sync_started_at, latest_page_modified),
                )
                await self._session.commit()
                logger.info("Notebook '%s' synced successfully", notebook.display_name)
            except Exception:
                logger.exception("Failed to sync notebook '%s'", notebook.display_name)
                await self._notebook_repo.update(
                    notebook.id,
                    NotebookUpdate(sync_status=NotebookSyncStatus.FAILED),
                )
                await self._session.commit()

    async def _sync_notebook(self, notebook: NotebookResponse, access_token: str) -> datetime | None:
        """Sync a notebook's sections + pages. Returns the newest page lastModifiedDateTime
        across the whole notebook (None if it has no pages) — the accurate "last edited"
        signal, which the caller writes onto the notebook when finalising it FRESH."""
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

        section_pages = await self._fetch_pages_for_sections(db_sections, access_token)

        latest_page_modified: datetime | None = None
        pages_to_sync: list[PageContentSyncCandidate] = []
        for section_page_list in section_pages:
            section_plan = await self._sync_section_metadata(
                section_page_list.section,
                section_page_list.graph_pages,
                notebook.last_synced_at,
            )
            if (
                section_plan.latest_page_modified is not None
                and (
                    latest_page_modified is None
                    or section_plan.latest_page_modified > latest_page_modified
                )
            ):
                latest_page_modified = section_plan.latest_page_modified
            pages_to_sync.extend(section_plan.pages_to_sync)

        await self._sync_page_contents(pages_to_sync, access_token)

        return latest_page_modified

    async def _fetch_pages_for_sections(
        self, sections: list[SectionResponse], access_token: str
    ) -> list[SectionPages]:
        async def fetch_one(section: SectionResponse) -> SectionPages:
            graph_pages = await self._graph_client.get_pages(access_token, section.onenote_id)
            return SectionPages(section=section, graph_pages=graph_pages)

        if not sections:
            return []
        # Concurrency here is bounded by the Graph semaphore inside GraphClient (5 concurrent
        # requests), so we gather all sections rather than adding a second semaphore.
        logger.info("  Fetching page lists for %d section(s)", len(sections))
        return await asyncio.gather(*(fetch_one(section) for section in sections))

    async def _sync_section_metadata(
        self,
        section: SectionResponse,
        graph_pages: list[GraphPage],
        notebook_last_synced_at: datetime | None,
    ) -> SectionSyncPlan:
        """Upsert/delete page metadata and return pages that need content sync."""
        existing_db_pages = await self._page_repo.list_by_section(section.id)

        if not graph_pages:
            logger.info("    Section '%s': no pages in Graph", section.display_name)
            pages_to_delete = [page.id for page in existing_db_pages]
            if pages_to_delete:
                await self._page_repo.delete_many(pages_to_delete)
            await self._session.commit()
            return SectionSyncPlan()

        latest_page_modified = max(graph_page.last_modified_datetime for graph_page in graph_pages)
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
        await self._session.commit()

        if self._force:
            logger.info("    Force mode — syncing all pages regardless of modification time")
        else:
            logger.info("    Last notebook sync: %s", notebook_last_synced_at or "never")

        to_sync: list[PageContentSyncCandidate] = []
        skipped = []
        for graph_page in graph_pages:
            db_page = db_pages_map.get(graph_page.id)
            if db_page and (
                self._force
                or notebook_last_synced_at is None
                or graph_page.last_modified_datetime > notebook_last_synced_at
                or db_page.sync_status == PageSyncStatus.FAILED
            ):
                to_sync.append(PageContentSyncCandidate(section_name=section.display_name, page=db_page))
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

        return SectionSyncPlan(latest_page_modified=latest_page_modified, pages_to_sync=to_sync)

    async def _sync_page_contents(
        self, candidates: list[PageContentSyncCandidate], access_token: str
    ) -> None:
        if not candidates:
            return

        semaphore = asyncio.Semaphore(self._page_worker_concurrency)

        async def build_one(candidate: PageContentSyncCandidate) -> PageContentSyncResult:
            async with semaphore:
                return await self._build_page_content_result(candidate.page, access_token)

        logger.info(
            "  Syncing content for %d page(s) with page-worker concurrency=%d",
            len(candidates),
            self._page_worker_concurrency,
        )
        tasks = [asyncio.create_task(build_one(candidate)) for candidate in candidates]
        completed = 0
        for task in asyncio.as_completed(tasks):
            result = await task
            completed += 1
            await self._apply_page_content_result(result)
            logger.info(
                "      Page content progress: %d/%d (%s -> %s)",
                completed,
                len(candidates),
                result.title or result.onenote_id,
                result.sync_status.value,
            )

    async def _build_page_content_result(
        self, page: PageResponse, access_token: str
    ) -> PageContentSyncResult:
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

            image_bytes_map = await self._fetch_page_images(image_elements, access_token)

            # Build composite canvas (images at CSS positions, ink strokes on top) and OCR it.
            # Single Vision call per page — the renderer clamps scale so the canvas fits Vision's cap.
            # Pillow is CPU-bound, so run it in a thread to avoid blocking the event loop (and the
            # other page workers) during decode/resize/paste/draw.
            composite_bytes = await asyncio.to_thread(
                composite_page, page_content.elements, image_bytes_map, page_content.ink_strokes
            )

            ocr_text = ""
            if composite_bytes is not None:
                if self._ocr_client is not None:
                    ocr_text = await self._ocr_client.run_ocr_async(composite_bytes)
                    logger.info("        Composite OCR: %d chars", len(ocr_text))
                else:
                    logger.info("        Composite built but OCR client not loaded — skipping")

            content = self._assemble_page_content(text_elements, ocr_text)
            logger.info("        Final content: %d chars", len(content))

            return PageContentSyncResult(
                page_id=page.id,
                title=page.title,
                onenote_id=page.onenote_id,
                content=content,
                content_hash=_compute_hash(content),
                sync_status=PageSyncStatus.FRESH,
            )
        except Exception as error:
            logger.exception("      Failed to sync content for page '%s'", page.title or page.onenote_id)
            return PageContentSyncResult(
                page_id=page.id,
                title=page.title,
                onenote_id=page.onenote_id,
                sync_status=PageSyncStatus.FAILED,
                error_message=str(error),
            )

    async def _fetch_page_images(
        self, image_elements: list[GraphPageElement], access_token: str
    ) -> dict[str, bytes]:
        image_bytes_map: dict[str, bytes] = {}
        urls = [element.image_url for element in image_elements if element.image_url]
        if not urls:
            return image_bytes_map

        results = await asyncio.gather(
            *[self._graph_client.get_page_image(access_token, url) for url in urls],
            return_exceptions=True,
        )
        for url, result in zip(urls, results):
            if isinstance(result, Exception):
                logger.warning("        Failed to fetch image: %s", result)
            else:
                image_bytes_map[url] = result
        return image_bytes_map

    def _assemble_page_content(
        self, text_elements: list[GraphPageElement], ocr_text: str
    ) -> str:
        text_parts = [element.text for element in text_elements if element.text]
        if ocr_text:
            text_parts.append(ocr_text)
        return "\n\n".join(text_parts)

    async def _apply_page_content_result(self, result: PageContentSyncResult) -> None:
        if result.sync_status == PageSyncStatus.FRESH and result.content is not None:
            await self._page_repo.update(
                result.page_id,
                PageUpdate(
                    content=result.content,
                    content_hash=result.content_hash,
                    sync_status=PageSyncStatus.FRESH,
                ),
            )
        else:
            if result.error_message:
                logger.warning(
                    "      Marking page '%s' failed: %s",
                    result.title or result.onenote_id,
                    result.error_message,
                )
            await self._page_repo.update(result.page_id, PageUpdate(sync_status=PageSyncStatus.FAILED))
        await self._session.commit()
