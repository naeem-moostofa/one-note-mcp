from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core.auth import get_current_user_id
from app.core.config import settings
from app.core.encryption import decrypt, encrypt
from app.routers.deps import get_auth_service
from app.schemas import MSALAuthCodeFlow
from app.services.auth_service import AuthService

router = APIRouter(prefix="/auth/microsoft", tags=["auth"])

_COOKIE_NAME = "oauth_flow"
_COOKIE_MAX_AGE = 600  # 10 minutes — enough for any login interaction
_COOKIE_SECURE = settings.MICROSOFT_REDIRECT_URI.startswith("https")


@router.get("/login")
async def login(
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RedirectResponse:
    flow = service.begin_login()
    response = RedirectResponse(url=flow.auth_uri, status_code=302)
    response.set_cookie(
        key=_COOKIE_NAME,
        value=encrypt(flow.model_dump_json()),
        httponly=True,
        samesite="lax",
        secure=_COOKIE_SECURE,
        max_age=_COOKIE_MAX_AGE,
    )
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
