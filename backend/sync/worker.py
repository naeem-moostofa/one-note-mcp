"""Single-executor sync worker (Phase 2).

This is the **only** process that should construct `SyncService` / touch `GraphClient`, so the
in-memory Graph rate limiter governs one executor (see plans/sync-rate-limit-fix-plan.md). It
drains the `sync_jobs` queue: claim the highest-priority due job (`FOR UPDATE SKIP LOCKED`), run
it, then finalise it — succeeded, retried with backoff, or failed. A heartbeat keeps the job's
lease fresh while it runs; a reaper requeues jobs orphaned by a crash and reconciles their
notebooks out of SYNCING.

Run it standalone:  python -m sync.worker

Single-executor invariant: exactly one replica, one process. Two workers — or a multi-process
server — means two independent rate limiters = 2x the per-user budget = the 429s come back. The
queue itself stays safe (SKIP LOCKED); the limiter does not.
"""

import argparse
import asyncio
import logging
import random
import signal
import sys
from datetime import datetime, timezone

from app.clients.graph_client import GraphClient
from app.clients.msal_client import get_msal_client
from app.clients.ocr_client import get_ocr_client
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import NotebookSyncStatus, SyncJobKind, SyncJobSource
from app.repositories.notebook_repository import NotebookRepository
from app.repositories.sync_job_repository import SyncJobRepository
from app.schemas import NotebookUpdate, SyncJobResponse
from app.services.sync_service import SyncService, _build_fresh_notebook_update

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff with jitter for a retryable job failure.

    ``attempts`` is the number of tries so far (>=1, already incremented at claim). Delay grows
    base * 2**(attempts-1), capped, plus up to one base of jitter to spread retries."""
    base = settings.SYNC_JOB_RETRY_BASE_S
    capped = min(settings.SYNC_JOB_RETRY_CAP_S, base * (2 ** max(0, attempts - 1)))
    return capped + random.uniform(0, base)


class SyncWorker:
    def __init__(self) -> None:
        self._shutdown = asyncio.Event()
        self._graph_client: GraphClient | None = None
        self._msal_client = None
        self._ocr_client = None

    def request_shutdown(self) -> None:
        """Cooperatively stop the run loop — used when the worker is embedded in another
        process (e.g. the API's lifespan) and shutdown is driven by that host, not signals."""
        self._shutdown.set()

    async def run(self, *, install_signal_handlers: bool = True) -> None:
        logger.info("Sync worker starting (sole Graph executor)")
        if install_signal_handlers:
            self._install_signal_handlers()
        async with GraphClient() as graph_client:
            self._graph_client = graph_client
            self._msal_client = get_msal_client()
            self._ocr_client = get_ocr_client()

            await self._reap()  # startup recovery: requeue jobs orphaned by a previous crash
            loop = asyncio.get_running_loop()
            last_reap = loop.time()

            while not self._shutdown.is_set():
                if loop.time() - last_reap >= settings.SYNC_REAPER_INTERVAL_S:
                    await self._reap()
                    last_reap = loop.time()

                job = await self._claim()
                if job is None:
                    await self._sleep_or_shutdown(settings.SYNC_WORKER_POLL_INTERVAL_S)
                    continue

                logger.info(
                    "Claimed job %d (%s, notebook=%s, attempt %d/%d)",
                    job.id, job.kind.value, job.notebook_id, job.attempts, job.max_attempts,
                )
                await self._execute(job)

        logger.info("Sync worker stopped")

    # --- claim / execute ---------------------------------------------------------------

    async def _claim(self) -> SyncJobResponse | None:
        async with AsyncSessionLocal() as session:
            job = await SyncJobRepository(session).claim_next()
            await session.commit()
            return job

    async def _execute(self, job: SyncJobResponse) -> None:
        """Run one claimed job under a heartbeat that keeps its lease alive."""
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(job.id, stop_heartbeat))
        try:
            if job.kind == SyncJobKind.NOTEBOOK_CONTENT:
                await self._run_notebook_content(job)
            else:
                await self._run_discovery(job)
        except Exception as error:  # defensive — handlers below already finalise their own failures
            logger.exception("Job %d crashed unexpectedly", job.id)
            await self._fail_or_retry(job, error)
        finally:
            stop_heartbeat.set()
            await heartbeat

    async def _run_notebook_content(self, job: SyncJobResponse) -> None:
        async with AsyncSessionLocal() as session:
            service = self._build_service(session)
            job_repo = SyncJobRepository(session)
            notebook_repo = NotebookRepository(session)

            notebook = await notebook_repo.get_by_id(job.notebook_id)
            if notebook is None or not notebook.sync_enabled:
                # sync_enabled toggled off / notebook deleted after enqueue — don't spend budget.
                await job_repo.mark_cancelled(job.id, "notebook missing or sync disabled at claim")
                await session.commit()
                logger.info("Job %d cancelled — notebook %s not syncable", job.id, job.notebook_id)
                return

            sync_started_at = datetime.now(timezone.utc)
            await notebook_repo.update(job.notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.SYNCING))
            await session.commit()

            try:
                latest_page_modified = await service.sync_notebook_content(job.notebook_id)
                await notebook_repo.update(
                    job.notebook_id, _build_fresh_notebook_update(sync_started_at, latest_page_modified)
                )
                await job_repo.mark_succeeded(job.id)
                await session.commit()
                logger.info("Job %d succeeded — notebook '%s' FRESH", job.id, notebook.display_name)
            except Exception as error:
                await session.rollback()
                logger.exception("Job %d failed syncing notebook '%s'", job.id, notebook.display_name)
                await self._fail_or_retry(job, error)

    async def _run_discovery(self, job: SyncJobResponse) -> None:
        async with AsyncSessionLocal() as session:
            service = self._build_service(session)
            job_repo = SyncJobRepository(session)
            try:
                notebooks = await service.discover_notebooks(job.connection_id)
                await session.commit()  # persist the upserted/pruned notebook list

                enabled = [notebook for notebook in notebooks if notebook.sync_enabled]
                fanned_out = 0
                for notebook in enabled:
                    created = await job_repo.enqueue(
                        kind=SyncJobKind.NOTEBOOK_CONTENT,
                        connection_id=job.connection_id,
                        user_id=job.user_id,
                        notebook_id=notebook.id,
                        source=SyncJobSource.AUTO,
                        priority=0,
                    )
                    if created is not None:
                        fanned_out += 1
                await job_repo.mark_succeeded(job.id)
                await session.commit()
                logger.info(
                    "Job %d (discovery) succeeded — %d enabled notebook(s), %d new content job(s)",
                    job.id, len(enabled), fanned_out,
                )
            except Exception as error:
                await session.rollback()
                logger.exception("Job %d (discovery) failed", job.id)
                await self._fail_or_retry(job, error)

    async def _fail_or_retry(self, job: SyncJobResponse, error: Exception) -> None:
        """Reschedule a job with backoff if it has retries left, else fail it terminally.

        On terminal failure of a content job, reconcile the notebook to FAILED so it doesn't
        sit in SYNCING forever; while retries remain we leave it SYNCING (the job is still
        queued)."""
        async with AsyncSessionLocal() as session:
            job_repo = SyncJobRepository(session)
            if job.attempts >= job.max_attempts:
                await job_repo.mark_failed(job.id, str(error))
                if job.notebook_id is not None:
                    await NotebookRepository(session).update(
                        job.notebook_id, NotebookUpdate(sync_status=NotebookSyncStatus.FAILED)
                    )
                logger.warning("Job %d failed terminally after %d attempts: %s", job.id, job.attempts, error)
            else:
                delay = retry_delay_seconds(job.attempts)
                await job_repo.reschedule(job.id, str(error), delay)
                logger.info("Job %d will retry in %.0fs (attempt %d/%d)", job.id, delay, job.attempts, job.max_attempts)
            await session.commit()

    # --- heartbeat / reaper ------------------------------------------------------------

    async def _heartbeat_loop(self, job_id: int, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.SYNC_JOB_HEARTBEAT_S)
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                return
            try:
                async with AsyncSessionLocal() as session:
                    await SyncJobRepository(session).heartbeat(job_id)
                    await session.commit()
            except Exception:
                logger.exception("Heartbeat failed for job %d", job_id)

    async def _reap(self) -> None:
        async with AsyncSessionLocal() as session:
            result = await SyncJobRepository(session).reap_expired()
            if result.failed_notebook_ids:
                await NotebookRepository(session).update_many(
                    result.failed_notebook_ids, NotebookUpdate(sync_status=NotebookSyncStatus.FAILED)
                )
            await session.commit()
        if result.requeued_ids or result.failed_notebook_ids:
            logger.info(
                "Reaper recovered orphans — requeued %d, failed %d",
                len(result.requeued_ids), len(result.failed_notebook_ids),
            )

    # --- wiring ------------------------------------------------------------------------

    def _build_service(self, session) -> SyncService:
        return SyncService(
            session=session,
            graph_client=self._graph_client,
            msal_client=self._msal_client,
            ocr_client=self._ocr_client,
        )

    async def _sleep_or_shutdown(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown.set)
            except NotImplementedError:
                # Windows event loop doesn't support add_signal_handler for SIGTERM — KeyboardInterrupt
                # (SIGINT) still propagates out of asyncio.run, which is enough for local dev.
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="OneNote MCP sync worker — the sole Graph executor")
    parser.parse_args()
    try:
        asyncio.run(SyncWorker().run())
    except KeyboardInterrupt:
        logger.info("Sync worker interrupted — exiting")


if __name__ == "__main__":
    main()
