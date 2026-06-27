from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_HOST: str = "127.0.0.1"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "onenote_mcp"

    TOKEN_ENCRYPTION_KEY: str
    APP_SESSION_SECRET: str
    MICROSOFT_CLIENT_ID: str
    MICROSOFT_CLIENT_SECRET: str
    MICROSOFT_AUTHORITY: str
    MICROSOFT_REDIRECT_URI: str
    MICROSOFT_SCOPES: str
    FRONTEND_ORIGIN: str
    GOOGLE_CLOUD_VISION_API_KEY: str
    MCP_SERVER_URL: str
    SYNC_PAGE_WORKER_CONCURRENCY: int = 1
    SYNC_GRAPH_CONCURRENCY: int = 5
    # OneNote per-app-per-user request limits; graph_client enforces these per Microsoft
    # connection. The documented maxima are 120/min and 400/hr, but the per-image `$value`
    # route throttles below those, so the per-minute knob is set under the documented max
    # to stop 429s clustering at the window edge. Tuning direction is *down*, never up.
    SYNC_GRAPH_RATE_PER_MINUTE: int = 90
    SYNC_GRAPH_RATE_PER_HOUR: int = 400
    SYNC_GRAPH_BUDGET_IDLE_EVICT_S: float = 30 * 60
    SYNC_GRAPH_BUDGET_EVICT_INTERVAL_S: float = 60
    SYNC_VISION_CONCURRENCY: int = 10

    # PDF file-printout extraction (see plans/attachment-fetch-optimization.md). A PDF printout's
    # source file is fetched once and text is pulled locally with PyMuPDF; a page falls back to
    # Vision OCR only when its embedded text is shorter than the threshold (a figure/scan page).
    SYNC_PDF_OCR_TEXT_THRESHOLD: int = 50          # chars; below this a page is treated as figure-only
    SYNC_PDF_RENDER_DPI: int = 150                 # local render scale for OCR'd pages (probe default; not a tuned optimum)

    # Durable sync-job queue (Phase 2). The worker is the *single* Graph executor; these tune
    # how it drains `sync_jobs`. See plans/sync-rate-limit-fix-plan.md (single-executor invariant).
    # Run the sync worker *inside* the API process (as a lifespan task) instead of as a separate
    # `python -m sync.worker`. Lets a UI sync start almost immediately without a second process.
    # SINGLE-EXECUTOR INVARIANT: only safe when exactly one web replica runs AND you are NOT also
    # running the standalone worker or the cron — otherwise you get multiple Graph rate limiters and
    # the 429 storm returns. Keep this False in multi-replica/production (use the dedicated worker
    # service there); enable it for single-process local dev. See plans/sync-rate-limit-fix-plan.md.
    SYNC_WORKER_IN_PROCESS: bool = False

    SYNC_WORKER_POLL_INTERVAL_S: float = 5.0   # idle sleep between empty claim attempts
    SYNC_JOB_LEASE_S: float = 120.0            # lease length; a crashed worker's jobs reap after this
    SYNC_JOB_HEARTBEAT_S: float = 30.0         # how often a running job refreshes its lease
    SYNC_JOB_MAX_ATTEMPTS: int = 5             # per-job retry budget before it is marked failed
    SYNC_JOB_RETRY_BASE_S: float = 30.0        # backoff base; delay = base * 2**(attempt-1) + jitter
    SYNC_JOB_RETRY_CAP_S: float = 900.0        # backoff ceiling
    SYNC_REAPER_INTERVAL_S: float = 60.0       # how often the worker sweeps for expired-lease orphans

    @computed_field
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


settings = Settings()  # type: ignore[call-arg]
