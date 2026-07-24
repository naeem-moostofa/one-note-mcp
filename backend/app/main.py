import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from mcp.server.auth.routes import cors_middleware
from starlette.routing import Route

from app.clients.graph_client import GraphClient
from app.core.config import settings
from app.core.exceptions import AppError
from app.mcp.server import mcp_app
from app.routers import auth, mcp_connections, me, notebooks, oauth_bridge
from sync.worker import SyncWorker

logger = logging.getLogger(__name__)


class _CanonicalIssuer:
    """Name the authorization server exactly as the authorization server names itself.

    Pydantic's AnyHttpUrl appends a trailing slash to a bare origin, so our
    Protected Resource Metadata advertises `https://…authkit.app/` while AuthKit's
    own metadata declares `issuer: https://…authkit.app`. RFC 8414 has the client
    compare those two strings, and a client that rejects the mismatch never reads
    the `registration_endpoint` it needs to register itself.

    Wraps the route's ASGI app rather than its handler, because FastMCP has already
    wrapped these routes in CORS middleware — so the body is only reachable from
    outside, as response messages.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start: dict = {}
        chunks: list[bytes] = []

        async def capture(message) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
                return
            if message["type"] != "http.response.body":
                await send(message)
                return

            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return

            body = b"".join(chunks)
            try:
                document = json.loads(body)
                servers = document.get("authorization_servers")
                if servers:
                    document["authorization_servers"] = [
                        str(server).rstrip("/") for server in servers
                    ]
                    body = json.dumps(document).encode()
            except (ValueError, AttributeError):
                pass  # not the JSON we expected — pass the original through untouched

            headers = [
                (key, value)
                for key, value in start.get("headers", [])
                if key.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(body)).encode()))
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, capture)


def _is_mcp_path(path: str) -> bool:
    return path.startswith("/mcp") or path.startswith("/.well-known/")


class FrontendCORSMiddleware(CORSMiddleware):
    """The frontend's credentialed CORS policy, applied to everything but MCP.

    Starlette answers preflights itself, before routing, so this policy's
    single-origin allowlist would reject preflights for the MCP transport and
    discovery routes — which carry their own permissive, credential-less CORS
    and are meant to be reachable from any web client's origin.
    """

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and _is_mcp_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def _with_cors(route: Route) -> Route:
    """Wrap a discovery route in permissive CORS unless it already carries it.

    FastMCP wraps the Protected Resource Metadata routes but not AuthKitProvider's
    forwarded authorization-server metadata, which it registers as a bare GET
    route. That one answers a browser with no Access-Control-Allow-Origin and
    405s the preflight, and web clients discover from a page context — a blocked
    fetch surfaces there as an opaque network error, not an HTTP status.
    """
    if isinstance(route.endpoint, CORSMiddleware):
        return route
    methods = sorted({*(route.methods or {"GET"}), "OPTIONS"})
    return Route(route.path, endpoint=cors_middleware(route.endpoint, methods), methods=methods)


def _expose_mcp_well_known_at_root(application: FastAPI) -> None:
    """Serve the MCP auth provider's OAuth discovery documents at the host root.

    The MCP app is mounted under /mcp, but RFC 9728 (Protected Resource Metadata)
    and RFC 8414 (Authorization Server Metadata) require these `.well-known`
    documents at the host root — that's where claude.ai / ChatGPT look during
    discovery, and where our 401 challenge points. The provider's routes are
    stateless metadata generators, so we re-expose them at root (a no-op when
    WorkOS isn't configured, since the onmcp_-only verifier adds no such routes).

    Each is CORS-wrapped in place first, so the mounted /mcp copies are fixed
    too and the root copies inherit it.
    """
    for index, route in enumerate(mcp_app.routes):
        if isinstance(route, Route) and route.path.startswith("/.well-known/"):
            route = _with_cors(route)
            if "oauth-protected-resource" in route.path:
                route = Route(
                    route.path,
                    endpoint=_CanonicalIssuer(route.endpoint),
                    methods=route.methods,
                )
            mcp_app.routes[index] = route

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
    FrontendCORSMiddleware,
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
