import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.routing import Route

from app.clients.graph_client import GraphClient
from app.core.config import settings
from app.core.exceptions import AppError
from app.mcp.server import mcp_app
from app.routers import auth, mcp_connections, me, notebooks, oauth_bridge
from sync.worker import SyncWorker

logger = logging.getLogger(__name__)


def _expose_mcp_well_known_at_root(application: FastAPI) -> None:
    """Serve the MCP auth provider's OAuth discovery documents at the host root.

    The MCP app is mounted under /mcp, but RFC 9728 (Protected Resource Metadata)
    and RFC 8414 (Authorization Server Metadata) require these `.well-known`
    documents at the host root — that's where claude.ai / ChatGPT look during
    discovery, and where our 401 challenge points. The provider's routes are
    stateless metadata generators, so we re-expose them at root (a no-op when
    WorkOS isn't configured, since the onmcp_-only verifier adds no such routes).
    """
    seen: set[str] = set()
    for route in mcp_app.routes:
        path = getattr(route, "path", "")
        if not (isinstance(route, Route) and path.startswith("/.well-known/")):
            continue
        if path not in seen:
            application.router.routes.append(Route(path, endpoint=route.endpoint, methods=route.methods))
            seen.add(path)
        # claude.ai also probes the bare PRM path without the resource-path suffix.
        bare = "/.well-known/oauth-protected-resource"
        if path.startswith(bare) and bare not in seen:
            application.router.routes.append(Route(bare, endpoint=route.endpoint, methods=route.methods))
            seen.add(bare)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FastMCP's streamable-http transport requires its lifespan to run so the
    # session manager is initialised — Starlette's mount() doesn't propagate
    # nested lifespans, so we drive it from ours.
    async with GraphClient() as graph_client:
        app.state.graph_client = graph_client

        # Optionally drain the sync queue in-process so a UI sync starts right away without a
        # separate `python -m sync.worker`. Gated by config because it only preserves the
        # single-Graph-executor invariant at one web replica with no standalone worker/cron.
        worker = SyncWorker() if settings.SYNC_WORKER_IN_PROCESS else None
        worker_task = (
            asyncio.create_task(worker.run(install_signal_handlers=False)) if worker else None
        )
        if worker:
            logger.info("In-process sync worker enabled (SYNC_WORKER_IN_PROCESS=True)")

        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            if worker and worker_task:
                worker.request_shutdown()
                await worker_task


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(oauth_bridge.router)
app.include_router(me.router)
app.include_router(notebooks.router)
app.include_router(mcp_connections.router)
app.mount("/mcp", mcp_app)
_expose_mcp_well_known_at_root(app)


# Maps domain errors → HTTP via each error's own status_code.
@app.exception_handler(AppError)
async def _app_error_handler(request, error: AppError):
    return JSONResponse(status_code=error.status_code, content={"detail": str(error) or error.__class__.__name__})
