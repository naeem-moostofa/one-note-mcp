"""Cron / CLI entry point — a *producer* for the durable sync queue (Phase 2).

By default this enqueues jobs and exits; the worker (`python -m sync.worker`) is the sole process
that talks to Graph, so the rate limiter governs one executor. Usage:

  python -m sync.run                     # enqueue a discovery job per active connection (cron)
  python -m sync.run --notebook-id 42    # enqueue one notebook content job
  python -m sync.run ... --run-inline    # debug: bypass the queue and sync directly in-process

`--run-inline` reproduces the pre-queue behaviour and is for local debugging ONLY — it makes Graph
calls from this process, so it must never run while the worker is also running (two executors =
two rate limiters = the 429s come back).
"""

import argparse
import asyncio
import logging

from app.clients.graph_client import GraphClient
from app.clients.msal_client import get_msal_client
from app.clients.ocr_client import get_ocr_client
from app.core.database import AsyncSessionLocal
from app.models import SyncJobKind, SyncJobSource
from app.repositories.microsoft_connection_repository import MicrosoftConnectionRepository
from app.repositories.notebook_repository import NotebookRepository
from app.repositories.sync_job_repository import SyncJobRepository
from app.services.sync_service import SyncService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def enqueue_jobs(notebook_id: int | None) -> None:
    """Producer path: stage jobs for the worker to drain, then exit."""
    async with AsyncSessionLocal() as session:
        job_repo = SyncJobRepository(session)
        if notebook_id is not None:
            notebook = await NotebookRepository(session).get_by_id(notebook_id)
            if notebook is None:
                logger.error("Notebook %s not found — nothing enqueued", notebook_id)
                return
            connection = await MicrosoftConnectionRepository(session).get_by_user_id(notebook.user_id)
            if connection is None:
                logger.error("No Microsoft connection for notebook %s — nothing enqueued", notebook_id)
                return
            created = await job_repo.enqueue(
                kind=SyncJobKind.NOTEBOOK_CONTENT,
                connection_id=connection.id,
                user_id=notebook.user_id,
                notebook_id=notebook_id,
                source=SyncJobSource.CLI,
            )
            await session.commit()
            logger.info(
                "Enqueued content job for notebook %s" if created else
                "Notebook %s already has an active job — nothing enqueued",
                notebook_id,
            )
            return

        connections = await MicrosoftConnectionRepository(session).get_all_active()
        enqueued = 0
        for connection in connections:
            created = await job_repo.enqueue(
                kind=SyncJobKind.DISCOVERY,
                connection_id=connection.id,
                user_id=connection.user_id,
                source=SyncJobSource.AUTO,
            )
            if created is not None:
                enqueued += 1
        await session.commit()
        logger.info("Enqueued %d discovery job(s) across %d active connection(s)", enqueued, len(connections))


async def run_inline(notebooks_only: bool, notebook_id: int | None, force: bool) -> None:
    """Debug-only: do the sync directly in this process (the pre-queue behaviour)."""
    logger.warning("Running inline (queue bypassed) — do NOT use while the worker is running")
    async with GraphClient() as graph_client:
        async with AsyncSessionLocal() as session:
            try:
                service = SyncService(
                    session=session,
                    graph_client=graph_client,
                    msal_client=get_msal_client(),
                    ocr_client=None if notebooks_only else get_ocr_client(),
                    force=force,
                )
                if notebooks_only:
                    await service.sync_notebooks_only()
                elif notebook_id is not None:
                    await service.sync_single_notebook(notebook_id)
                else:
                    await service.run()
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception("Inline sync failed")
                raise
    logger.info("Inline sync complete")


def main() -> None:
    parser = argparse.ArgumentParser(description="OneNote MCP sync (queue producer)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--notebooks-only", action="store_true", help="Inline only: sync the notebook list, no content")
    group.add_argument("--notebook-id", type=int, metavar="ID", help="Sync a single notebook by DB id")
    parser.add_argument("--force", action="store_true", help="Inline only: sync all pages regardless of modification time")
    parser.add_argument(
        "--run-inline",
        action="store_true",
        help="Debug: bypass the queue and sync directly in-process (never run alongside the worker)",
    )
    args = parser.parse_args()

    if args.run_inline:
        asyncio.run(run_inline(notebooks_only=args.notebooks_only, notebook_id=args.notebook_id, force=args.force))
        return

    if args.notebooks_only or args.force:
        parser.error("--notebooks-only/--force only apply with --run-inline (the queue handles discovery)")
    asyncio.run(enqueue_jobs(notebook_id=args.notebook_id))


if __name__ == "__main__":
    main()
