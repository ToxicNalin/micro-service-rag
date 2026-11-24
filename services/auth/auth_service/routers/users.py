"""User management for admins. The tenant always comes from the caller's token (I1), never the request."""

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service import service
from auth_service.db import get_session
from auth_service.schemas import CreateUserRequest, UpdateUserRequest, UserOut
from common.security import Principal, require_admin

router = APIRouter(prefix="/users", tags=["users"])


@router.get("")
async def list_users_route(
    principal: Principal = Depends(require_admin), session: AsyncSession = Depends(get_session)
) -> list[UserOut]:
    """List the users of the caller's tenant."""
    users = await service.list_users(session, uuid.UUID(principal.tenant_id))
    return [UserOut.model_validate(u) for u in users]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user_route(
    req: CreateUserRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Create a user in the caller's tenant."""
    user = await service.create_user(session, uuid.UUID(principal.tenant_id), req)
    return UserOut.model_validate(user)


@router.patch("/{user_id}")
async def update_user_route(
    user_id: uuid.UUID,
    req: UpdateUserRequest,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    """Change a user's name, role or active flag (404 for users of other tenants)."""
    user = await service.update_user(session, uuid.UUID(principal.tenant_id), user_id, req, principal)
    return UserOut.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user_route(
    user_id: uuid.UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Deactivate a user (accounts are never hard-deleted)."""
    await service.deactivate_user(session, uuid.UUID(principal.tenant_id), user_id, principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
