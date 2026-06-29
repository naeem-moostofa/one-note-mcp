from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.core.auth import get_current_user_id
from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.routers.deps import get_auth_service
from app.schemas import MSALAuthCodeFlow
from app.services.auth_service import AuthService
from app.services.workos_bridge_service import WorkOSBridgeService

router = APIRouter(prefix="/auth/microsoft", tags=["auth"])

_COOKIE_NAME = "oauth_flow"
_COOKIE_MAX_AGE = 600  # 10 minutes — enough for any login interaction
_COOKIE_SECURE = settings.MICROSOFT_REDIRECT_URI.startswith("https")

# Bridge mode: the OAuth bridge (WorkOS Standalone Connect) reuses this same
# Microsoft login + callback, but the callback must return to WorkOS instead of
# the SPA. This cookie carries WorkOS's external_auth_id through the round-trip
# and marks the callback as bridge-bound. Set only by routers/oauth_bridge.py.
_BRIDGE_COOKIE_NAME = "oauth_bridge"


def set_oauth_flow_cookie(response: Response, flow: MSALAuthCodeFlow) -> None:
    """Persist the MSAL auth-code flow between redirect and callback."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=encrypt(flow.model_dump_json()),
        httponly=True,
        samesite="lax",
        secure=_COOKIE_SECURE,
        max_age=_COOKIE_MAX_AGE,
    )


@router.get("/login")
async def login(
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RedirectResponse:
    flow = service.begin_login()
    response = RedirectResponse(url=flow.auth_uri, status_code=302)
    set_oauth_flow_cookie(response, flow)
    return response


@router.get("/callback", responses={400: {"description": "OAuth error returned by Microsoft or invalid/expired state"}})
async def callback(
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RedirectResponse:
    auth_response = dict(request.query_params)

    if "error" in auth_response:
        raise HTTPException(status_code=400, detail=auth_response.get("error_description", "OAuth error"))

    encrypted_flow = request.cookies.get(_COOKIE_NAME)
    if not encrypted_flow:
        raise HTTPException(status_code=400, detail="Missing OAuth state cookie — login session may have expired")

    try:
        flow = MSALAuthCodeFlow.model_validate_json(decrypt(encrypted_flow))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid OAuth state cookie")

    # Bridge mode: a WorkOS-initiated login. Hand the deduped user back to WorkOS
    # and redirect to AuthKit to finish the OAuth flow, instead of the SPA.
    encrypted_bridge = request.cookies.get(_BRIDGE_COOKIE_NAME)
    if encrypted_bridge is not None:
        try:
            external_auth_id = decrypt(encrypted_bridge)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid OAuth bridge cookie")
        try:
            user = await service.complete_login_user(flow, auth_response)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        redirect_uri = await WorkOSBridgeService().complete_login(external_auth_id, user)
        response = RedirectResponse(url=redirect_uri, status_code=302)
        response.delete_cookie(key=_COOKIE_NAME)
        response.delete_cookie(key=_BRIDGE_COOKIE_NAME)
        return response

    try:
        token = await service.complete_login(flow, auth_response)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    response = RedirectResponse(url=f"{settings.FRONTEND_ORIGIN}?token={token}", status_code=302)
    response.delete_cookie(key=_COOKIE_NAME)
    return response


@router.post("/disconnect", status_code=204)
async def disconnect(
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    await service.disconnect(user_id)
