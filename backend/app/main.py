from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.clients.graph_client import GraphClient
from app.core.config import settings
from app.core.exceptions import AppError
from app.mcp.server import mcp_app
from app.routers import auth, mcp_connections, me, notebooks


@asynccontextmanager
async def lifespan(app: FastAPI):
    # FastMCP's streamable-http transport requires its lifespan to run so the
    # session manager is initialised — Starlette's mount() doesn't propagate
    # nested lifespans, so we drive it from ours.
    async with GraphClient() as graph_client:
        app.state.graph_client = graph_client
        async with mcp_app.lifespan(app):
            yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.FRONTEND_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(notebooks.router)
app.include_router(mcp_connections.router)
app.mount("/mcp", mcp_app)


# Maps domain errors → HTTP via each error's own status_code.
@app.exception_handler(AppError)
async def _app_error_handler(request, error: AppError):
    return JSONResponse(status_code=error.status_code, content={"detail": str(error) or error.__class__.__name__})
