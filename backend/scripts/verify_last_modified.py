"""
Live end-to-end check for the notebook "last edited" (last_modified_datetime) work.

Drives the REAL Microsoft Graph against ONE small notebook to verify:

  1. Refresh (names-only) does NOT write last_modified_datetime — it stays NULL.
  2. A content Sync sets last_modified_datetime to the *max page* lastModifiedDateTime
     across the whole notebook (the accurate "last edited" signal), not the unreliable
     notebook-level timestamp.
  3. sync_status → FRESH and last_synced_at is set after the sync.
  4. The web response (NotebookService.list_for_user) carries last_modified_datetime.

To stay cheap it scans notebooks' page counts (metadata only) and picks the smallest
non-empty notebook, or the first one with <= --small-threshold pages (stops early).
Override with --notebook-id to target a specific DB notebook.

Usage:
    uv run python -m scripts.verify_last_modified
    uv run python -m scripts.verify_last_modified --notebook-id 77
    uv run python -m scripts.verify_last_modified --no-ocr     # skip Vision (faster/cheaper)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timedelta

import httpx

from app.clients.graph_client import GraphClient
from app.clients.msal_client import get_msal_client
from app.clients.ocr_client import get_ocr_client
from app.core.database import AsyncSessionLocal, engine
from app.services.notebook_service import NotebookService
from app.services.sync_service import SyncService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("verify_last_modified")

# A notebook with at most this many pages counts as "small" — scanning stops at the
# first one that qualifies so we don't walk every notebook's pages.
DEFAULT_SMALL_THRESHOLD = 5


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"FAIL: {message}")
    log.info("  OK: %s", message)


def _close(left: datetime | None, right: datetime | None) -> bool:
    """Datetime equality with a 1s tolerance (timestamptz round-trip / Graph precision)."""
    if left is None or right is None:
        return left is right
    return abs(left - right) < timedelta(seconds=1)


async def _pick_notebook(service: SyncService, access_token: str, small_threshold: int):
    """Scan Graph (metadata only) and return (graph_notebook, page_count, max_page_modified)
    for the smallest non-empty notebook. Stops early at the first <= small_threshold."""
    graph_notebooks = await service._graph_client.get_notebooks(access_token)
    log.info("Scanning %d notebooks for the smallest non-empty one…", len(graph_notebooks))

    best = None  # (graph_notebook, count, max_modified)
    for graph_notebook in graph_notebooks:
        sections = await service._graph_client.get_sections(access_token, graph_notebook.id)
        pages = []
        for section in sections:
            pages.extend(await service._graph_client.get_pages(access_token, section.id))
        count = len(pages)
        if count == 0:
            continue
        max_modified = max(page.last_modified_datetime for page in pages)
        log.info("  '%s': %d page(s), newest %s", graph_notebook.display_name, count, max_modified.isoformat())
        if best is None or count < best[1]:
            best = (graph_notebook, count, max_modified)
        if count <= small_threshold:
            log.info("  -> picking '%s' (<= %d pages)", graph_notebook.display_name, small_threshold)
            return best

    if best is None:
        raise SystemExit("FAIL: no non-empty notebooks found to test against")
    log.info("  -> picking smallest '%s' (%d pages)", best[0].display_name, best[1])
    return best


async def _run(notebook_id_override: int | None, small_threshold: int, use_ocr: bool) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http_client:
        async with AsyncSessionLocal() as session:
            service = SyncService(
                session=session,
                graph_client=GraphClient(http_client),
                msal_client=get_msal_client(),
                ocr_client=get_ocr_client() if use_ocr else None,
            )
            notebook_service = NotebookService(session)

            connections = await service._connection_repo.get_all_active()
            _assert(len(connections) >= 1, f"found an active Microsoft connection (got {len(connections)})")
            connection = connections[0]
            user_id = connection.user_id

            access_token = await service._acquire_token(connection)
            _assert(access_token is not None, "acquired a Graph access token (account still connected)")

            # --- Phase 1: Refresh must NOT touch last_modified_datetime ---
            log.info("Phase 1: refresh_notebook_list (names only)…")
            await service.refresh_notebook_list(user_id)
            await session.commit()

            db_notebooks = await service._notebook_repo.list_by_user(user_id)
            _assert(len(db_notebooks) >= 1, f"user has notebooks after refresh (got {len(db_notebooks)})")

            # Resolve which notebook to sync.
            if notebook_id_override is not None:
                chosen_db = next((nb for nb in db_notebooks if nb.id == notebook_id_override), None)
                _assert(chosen_db is not None, f"--notebook-id {notebook_id_override} belongs to the connected user")
                graph_notebooks = await service._graph_client.get_notebooks(access_token)
                graph_notebook = next((gn for gn in graph_notebooks if gn.id == chosen_db.onenote_id), None)
                _assert(graph_notebook is not None, "the chosen notebook still exists in Graph")
                sections = await service._graph_client.get_sections(access_token, graph_notebook.id)
                pages = []
                for section in sections:
                    pages.extend(await service._graph_client.get_pages(access_token, section.id))
                _assert(len(pages) >= 1, f"chosen notebook has pages to sync (got {len(pages)})")
                expected_max = max(page.last_modified_datetime for page in pages)
            else:
                graph_notebook, _count, expected_max = await _pick_notebook(service, access_token, small_threshold)
                chosen_db = next((nb for nb in db_notebooks if nb.onenote_id == graph_notebook.id), None)
                _assert(chosen_db is not None, "picked notebook is present in the DB after refresh")

            refreshed = next(nb for nb in db_notebooks if nb.id == chosen_db.id)
            _assert(
                refreshed.last_modified_datetime is None,
                f"after Refresh, last_modified_datetime is still NULL (got {refreshed.last_modified_datetime})",
            )

            log.info(
                "Target notebook: '%s' (db id %d) — expected last edited %s",
                chosen_db.display_name, chosen_db.id, expected_max.isoformat(),
            )

            # --- Phase 2: Sync must set last_modified_datetime to the max page edit time ---
            log.info("Phase 2: sync_single_notebook(%d)%s…", chosen_db.id, "" if use_ocr else " (OCR disabled)")
            await service.sync_single_notebook(chosen_db.id)
            await session.commit()

            synced = await service._notebook_repo.get_by_id(chosen_db.id)
            _assert(synced is not None, "notebook still present after sync")
            _assert(
                synced.sync_status.value == "FRESH",
                f"sync_status is FRESH after a successful sync (got {synced.sync_status.value})",
            )
            _assert(synced.last_synced_at is not None, "last_synced_at is set after sync")
            _assert(
                synced.last_modified_datetime is not None,
                "last_modified_datetime is populated after sync (no longer NULL)",
            )
            _assert(
                _close(synced.last_modified_datetime, expected_max),
                f"last_modified_datetime == max page edit time "
                f"(got {synced.last_modified_datetime}, expected {expected_max})",
            )
            _assert(
                synced.last_modified_datetime != synced.last_synced_at,
                "last_modified_datetime (page edit time) is distinct from last_synced_at (sync time)",
            )

            # --- Phase 3: web response carries the field ---
            log.info("Phase 3: NotebookService.list_for_user web response shape…")
            web = await notebook_service.list_for_user(user_id)
            web_chosen = next((nb for nb in web if nb.id == chosen_db.id), None)
            _assert(web_chosen is not None, "synced notebook appears in the web response")
            _assert(
                hasattr(web_chosen, "last_modified_datetime") and _close(web_chosen.last_modified_datetime, expected_max),
                "web response last_modified_datetime matches the DB value",
            )

            # --- Phase 4: idempotent re-sync keeps the timestamp accurate ---
            log.info("Phase 4: re-sync (pages unchanged) keeps last_modified_datetime correct…")
            await service.sync_single_notebook(chosen_db.id)
            await session.commit()
            resynced = await service._notebook_repo.get_by_id(chosen_db.id)
            _assert(
                _close(resynced.last_modified_datetime, expected_max),
                "last_modified_datetime stays correct after a no-op re-sync (computed from all pages, not just changed ones)",
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Verify notebook last_modified_datetime behaviour")
    parser.add_argument("--notebook-id", type=int, default=None, help="Target a specific DB notebook id")
    parser.add_argument("--small-threshold", type=int, default=DEFAULT_SMALL_THRESHOLD, help="Pages count considered 'small'")
    parser.add_argument("--no-ocr", action="store_true", help="Skip Vision OCR (faster, still exercises the sync path)")
    args = parser.parse_args()

    try:
        await _run(args.notebook_id, args.small_threshold, use_ocr=not args.no_ocr)
        log.info("ALL CHECKS PASSED")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
