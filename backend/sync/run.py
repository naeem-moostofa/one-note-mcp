import argparse
import asyncio
import logging

from app.clients.graph_client import GraphClient
from app.clients.msal_client import get_msal_client
from app.clients.ocr_client import get_ocr_client
from app.core.database import AsyncSessionLocal
from app.services.sync_service import SyncService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main(notebooks_only: bool, notebook_id: int | None, force: bool = False) -> None:
    logger.info("Sync started")
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
                logger.exception("Sync failed")
                raise
    logger.info("Sync complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OneNote MCP sync")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--notebooks-only", action="store_true", help="Sync notebook list only — no sections or pages")
    group.add_argument("--notebook-id", type=int, metavar="ID", help="Sync a single notebook by DB id")
    parser.add_argument("--force", action="store_true", help="Sync all pages regardless of modification time")
    args = parser.parse_args()

    asyncio.run(main(notebooks_only=args.notebooks_only, notebook_id=args.notebook_id, force=args.force))
