from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import get_current_user_id
from app.routers.deps import get_user_service
from app.schemas import MeResponse
from app.services.user_service import UserService

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
async def get_me(
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[UserService, Depends(get_user_service)],
) -> MeResponse:
    return await service.get_profile(user_id)
