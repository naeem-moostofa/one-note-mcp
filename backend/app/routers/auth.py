from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core.auth import get_current_user_id
from app.core.config import settings
from app.routers.deps import get_auth_service
from app.services.auth_service import AuthService
from app.services.workos_bridge_service import WorkOSBridgeService

router = APIRouter(prefix="/auth/microsoft", tags=["auth"])


@router.get("/login")
async def login(
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RedirectResponse:
    flow = service.begin_login()
    await service.persist_flow(flow)
    return RedirectResponse(url=flow.auth_uri, status_code=302)


@router.get("/callback", responses={400: {"description": "OAuth error returned by Microsoft or unknown/expired state"}})
async def callback(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RedirectResponse:
    auth_response = dict(request.query_params)

    if "error" in auth_response:
        raise HTTPException(status_code=400, detail=auth_response.get("error_description", "OAuth error"))

    state = auth_response.get("state")
    if not state:
        raise HTTPException(status_code=400, detail="Missing OAuth state")

    # The in-flight login is recovered server-side by `state` (no cookie) — this is
    # what lets the WorkOS bridge survive its cross-site redirect bounce.
    recovered = await service.pop_flow(state)
    if recovered is None:
        raise HTTPException(status_code=400, detail="Login session not found or expired — please start again")
    flow, external_auth_id = recovered

    # Bridge mode: a WorkOS-initiated login. Hand the deduped user back to WorkOS
    # and redirect to AuthKit to finish the OAuth flow, instead of the SPA.
    if external_auth_id is not None:
        try:
            user = await service.complete_login_user(flow, auth_response)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        redirect_uri = await WorkOSBridgeService().complete_login(external_auth_id, user)
        return RedirectResponse(url=redirect_uri, status_code=302)

    try:
        token = await service.complete_login(flow, auth_response)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    return RedirectResponse(url=f"{settings.FRONTEND_ORIGIN}?token={token}", status_code=302)


@router.post("/disconnect", status_code=204)
async def disconnect(
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    await service.disconnect(user_id)
