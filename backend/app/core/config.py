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
    SYNC_PAGE_WORKER_CONCURRENCY: int = 10
    SYNC_GRAPH_CONCURRENCY: int = 5
    # Documented OneNote per-app-per-user request limits; the sliding-window limiter in
    # graph_client enforces both. Dial down if 429s cluster at a window edge.
    SYNC_GRAPH_RATE_PER_MINUTE: int = 120
    SYNC_GRAPH_RATE_PER_HOUR: int = 400
    SYNC_VISION_CONCURRENCY: int = 10

    # Durable sync-job queue (Phase 2). The worker is the *single* Graph executor; these tune
    # how it drains `sync_jobs`. See plans/sync-rate-limit-fix-plan.md (single-executor invariant).
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
