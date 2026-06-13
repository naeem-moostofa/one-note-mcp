from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.auth import get_current_user_id
from app.routers.deps import get_mcp_connection_service
from app.schemas import (
    MCPConnectionCreatedResponse,
    MCPConnectionCreateRequest,
    MCPConnectionResponse,
    MCPConnectionWebResponse,
)
from app.services.mcp_connection_service import MCPConnectionService

router = APIRouter(prefix="/api/mcp-connections", tags=["mcp-connections"])


@router.post("", status_code=201)
async def create_connection(
    body: MCPConnectionCreateRequest,
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[MCPConnectionService, Depends(get_mcp_connection_service)],
) -> MCPConnectionCreatedResponse:
    return await service.create(
        user_id=user_id,
        scope_all_notebooks=body.scope_all_notebooks,
        notebook_ids=body.notebook_ids,
        display_name=body.display_name,
    )


@router.get("", response_model=list[MCPConnectionWebResponse])
async def list_connections(
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[MCPConnectionService, Depends(get_mcp_connection_service)],
) -> list[MCPConnectionResponse]:
    # response_model (decorator kwarg) projects to the web shape, dropping
    # token_hash/user_id. The annotation reflects what we actually return.
    return await service.list_for_user(user_id)


@router.delete("/{connection_id}", status_code=204)
async def revoke_connection(
    connection_id: int,
    user_id: Annotated[int, Depends(get_current_user_id)],
    service: Annotated[MCPConnectionService, Depends(get_mcp_connection_service)],
) -> None:
    await service.revoke(user_id, connection_id)
