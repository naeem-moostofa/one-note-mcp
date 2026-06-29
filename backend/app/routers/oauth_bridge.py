"""WorkOS Standalone Connect bridge — the AuthKit "Login URI".

WorkOS (the OAuth AS for web MCP clients) handles DCR/CIMD/PKCE/token issuance,
then redirects the user here with an `external_auth_id` to authenticate against
*our* identity. We run the existing Microsoft login; the bridge-aware callback in
routers/auth.py then dedupes the user and calls WorkOS's completion API. This
route only kicks off that login and stashes the `external_auth_id` for the
callback. See plans/mcp-oauth-web-clients.md.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core.encryption import encrypt
from app.routers.auth import (
    _BRIDGE_COOKIE_NAME,
    _COOKIE_MAX_AGE,
    _COOKIE_SECURE,
    set_oauth_flow_cookie,
)
from app.routers.deps import get_auth_service
from app.services.auth_service import AuthService

router = APIRouter(prefix="/oauth/bridge", tags=["oauth-bridge"])


@router.get("/login")
async def login(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RedirectResponse:
    external_auth_id = request.query_params.get("external_auth_id")
    if not external_auth_id:
        raise HTTPException(status_code=400, detail="Missing external_auth_id")

    flow = service.begin_login()
    response = RedirectResponse(url=flow.auth_uri, status_code=302)
    # The callback reuses the standard oauth_flow cookie; the bridge cookie both
    # carries external_auth_id and signals bridge mode to the shared callback.
    set_oauth_flow_cookie(response, flow)
    response.set_cookie(
        key=_BRIDGE_COOKIE_NAME,
        value=encrypt(external_auth_id),
        httponly=True,
        samesite="lax",
        secure=_COOKIE_SECURE,
        max_age=_COOKIE_MAX_AGE,
    )
    return response
