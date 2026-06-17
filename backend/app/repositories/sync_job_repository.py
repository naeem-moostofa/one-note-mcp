"""Data access for the durable sync-job queue (Phase 2).

The queue is the single coordination point between sync *producers* (web routes, cron, CLI)
and the single *worker* that drains it. Atomicity guarantees live here:

- ``enqueue`` is idempotent via ``INSERT ... ON CONFLICT DO NOTHING`` against the partial unique
  indexes (one active job per notebook for content jobs, one per connection for discovery jobs).
- ``claim_next`` uses ``FOR UPDATE SKIP LOCKED`` so the claim is safe even if a second worker is
  ever started (the queue stays correct; only the in-memory rate limiter needs single-process).

Callers own the transaction boundary (commit/rollback) — these methods only stage statements,
mirroring the other repositories.
"""

from datetime import timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    SYNC_JOB_ACTIVE_DISCOVERY_WHERE,
    SYNC_JOB_ACTIVE_NOTEBOOK_WHERE,
    SyncJob,
    SyncJobKind,
    SyncJobSource,
    SyncJobStatus,
)
from app.schemas import ReapResult, SyncJobResponse

_ACTIVE_STATUSES = (SyncJobStatus.PENDING, SyncJobStatus.RUNNING)


class SyncJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, job_id: int) -> SyncJobResponse | None:
        row = await self.session.get(SyncJob, job_id)
        return SyncJobResponse.model_validate(row) if row else None

    async def enqueue(
        self,
        *,
        kind: SyncJobKind,
        connection_id: int,
        user_id: int,
        source: SyncJobSource,
        notebook_id: int | None = None,
        priority: int = 0,
        max_attempts: int = settings.SYNC_JOB_MAX_ATTEMPTS,
    ) -> SyncJobResponse | None:
        """Insert a new job, or no-op if an active (pending/running) one already exists.

        Returns the created job, or None when an active duplicate already existed — that return
        value is the dedup signal (spam-clicked syncs / overlapping cron runs collapse to one job).
        The conflict arbiter is chosen by kind: content jobs dedup per notebook, discovery jobs
        per connection (their notebook_id is NULL, so the per-notebook index never applies)."""
        if kind == SyncJobKind.DISCOVERY:
            conflict_kwargs = {
                "index_elements": ["connection_id", "kind"],
                "index_where": text(SYNC_JOB_ACTIVE_DISCOVERY_WHERE),
            }
        else:
            conflict_kwargs = {
                "index_elements": ["notebook_id", "kind"],
                "index_where": text(SYNC_JOB_ACTIVE_NOTEBOOK_WHERE),
            }

        statement = (
            pg_insert(SyncJob)
            .values(
                kind=kind,
                connection_id=connection_id,
                user_id=user_id,
                notebook_id=notebook_id,
                source=source,
                priority=priority,
                max_attempts=max_attempts,
                status=SyncJobStatus.PENDING,
            )
            .on_conflict_do_nothing(**conflict_kwargs)
            .returning(SyncJob.id)
        )
        new_id = await self.session.scalar(statement)
        if new_id is None:
            return None
        return await self.get_by_id(new_id)

    async def claim_next(self, lease_seconds: float = settings.SYNC_JOB_LEASE_S) -> SyncJobResponse | None:
        """Atomically claim the highest-priority due job and mark it RUNNING.

        FOR UPDATE SKIP LOCKED steps over any row another transaction already holds, so concurrent
        claimers never collide. Increments attempts and stamps a lease the reaper uses to recover
        the job if this worker dies mid-run."""
        job_id = await self.session.scalar(
            select(SyncJob.id)
            .where(SyncJob.status == SyncJobStatus.PENDING, SyncJob.next_run_at <= func.now())
            .order_by(SyncJob.priority.desc(), SyncJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job_id is None:
            return None
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(
                status=SyncJobStatus.RUNNING,
                attempts=SyncJob.attempts + 1,
                started_at=func.now(),
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
            )
        )
        return await self.get_by_id(job_id)

    async def heartbeat(self, job_id: int, lease_seconds: float = settings.SYNC_JOB_LEASE_S) -> None:
        """Extend a running job's lease so the reaper doesn't treat a long sync as a crash."""
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id, SyncJob.status == SyncJobStatus.RUNNING)
            .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
        )

    async def mark_succeeded(self, job_id: int) -> None:
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(status=SyncJobStatus.SUCCEEDED, finished_at=func.now(), lease_expires_at=None)
        )

    async def mark_failed(self, job_id: int, error: str) -> None:
        """Terminal failure — the retry budget is exhausted."""
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(
                status=SyncJobStatus.FAILED,
                finished_at=func.now(),
                last_error=error[:2000],
                lease_expires_at=None,
            )
        )

    async def reschedule(self, job_id: int, error: str, delay_seconds: float) -> None:
        """Put a failed-but-retryable job back to PENDING with backoff (attempts already bumped)."""
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(
                status=SyncJobStatus.PENDING,
                next_run_at=func.now() + timedelta(seconds=delay_seconds),
                last_error=error[:2000],
                lease_expires_at=None,
            )
        )

    async def mark_cancelled(self, job_id: int, reason: str) -> None:
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(
                status=SyncJobStatus.CANCELLED,
                finished_at=func.now(),
                last_error=reason[:2000],
                lease_expires_at=None,
            )
        )

    async def cancel_pending_for_notebook(self, notebook_id: int) -> None:
        """Cancel a notebook's queued (not-yet-running) jobs — used when sync is disabled/deleted.

        A running job is left alone; it cooperatively no-ops at claim time / on the next run."""
        await self.session.execute(
            update(SyncJob)
            .where(SyncJob.notebook_id == notebook_id, SyncJob.status == SyncJobStatus.PENDING)
            .values(status=SyncJobStatus.CANCELLED, finished_at=func.now(), last_error="cancelled")
        )

    async def reap_expired(self) -> ReapResult:
        """Recover jobs whose worker died (lease expired while RUNNING).

        Jobs with retries left are requeued (PENDING, due now); jobs that already burned their
        budget are failed terminally. The caller reconciles the failed jobs' notebooks to FAILED
        so a killed worker never strands a notebook in SYNCING."""
        failed_notebook_ids = (
            await self.session.scalars(
                update(SyncJob)
                .where(
                    SyncJob.status == SyncJobStatus.RUNNING,
                    SyncJob.lease_expires_at < func.now(),
                    SyncJob.attempts >= SyncJob.max_attempts,
                )
                .values(
                    status=SyncJobStatus.FAILED,
                    finished_at=func.now(),
                    last_error="lease expired (worker crash) — retries exhausted",
                    lease_expires_at=None,
                )
                .returning(SyncJob.notebook_id)
            )
        ).all()

        requeued_ids = (
            await self.session.scalars(
                update(SyncJob)
                .where(SyncJob.status == SyncJobStatus.RUNNING, SyncJob.lease_expires_at < func.now())
                .values(status=SyncJobStatus.PENDING, next_run_at=func.now(), lease_expires_at=None)
                .returning(SyncJob.id)
            )
        ).all()

        return ReapResult(
            requeued_ids=list(requeued_ids),
            failed_notebook_ids=[nid for nid in failed_notebook_ids if nid is not None],
        )
