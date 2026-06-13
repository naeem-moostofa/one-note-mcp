"""Shared FastAPI dependency providers that construct services for the routers.

These bridge FastAPI's request-scoped DI (session, msal_client, app.state) to the
framework-agnostic service constructors. Kept out of the service modules on
purpose: the services stay importable from non-HTTP entrypoints (e.g.
`app/mcp/tools.py` builds them directly), so the FastAPI wiring lives here.
"""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.msal_client import MSALClient, get_msal_client
from app.core.database import get_session
from app.services.auth_service import AuthService
from app.services.mcp_connection_service import MCPConnectionService
from app.services.notebook_service import NotebookService
from app.services.sync_service import SyncService
from app.services.user_service import UserService


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_session)],
    msal_client: Annotated[MSALClient, Depends(get_msal_client)],
) -> AuthService:
    return AuthService(session, msal_client)


def get_user_service(session: Annotated[AsyncSession, Depends(get_session)]) -> UserService:
    return UserService(session)


def get_notebook_service(session: Annotated[AsyncSession, Depends(get_session)]) -> NotebookService:
    return NotebookService(session)


def get_sync_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    msal_client: Annotated[MSALClient, Depends(get_msal_client)],
) -> SyncService:
    # graph_client is built in main.py's lifespan; a names-only refresh needs no ocr_client.
    return SyncService(session, request.app.state.graph_client, msal_client)


def get_mcp_connection_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> MCPConnectionService:
    return MCPConnectionService(session)
