"""WorkOS Standalone Connect bridge — the AuthKit "Login URI".

WorkOS (the OAuth AS for web MCP clients) handles DCR/CIMD/PKCE/token issuance,
then redirects the user here with an `external_auth_id` to authenticate against
*our* identity. We run the existing Microsoft login; the shared callback in
routers/auth.py then dedupes the user and calls WorkOS's completion API. This
route only kicks off that login, stashing `external_auth_id` alongside the flow
server-side (keyed by the OAuth `state`) — no cookie, so it survives the
cross-site redirect bounce through Microsoft. See plans/mcp-oauth-web-clients.md.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

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
    await service.persist_flow(flow, external_auth_id=external_auth_id)
    return RedirectResponse(url=flow.auth_uri, status_code=302)
