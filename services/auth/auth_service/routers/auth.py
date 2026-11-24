"""Sign-up, login, token refresh/logout, the caller's profile and password change."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service import service, tokens
from auth_service.config import get_settings
from auth_service.db import get_session
from auth_service.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    TenantOut,
    TokenResponse,
    UserOut,
)
from common.security import Principal, get_principal

router = APIRouter(prefix="/auth", tags=["auth"])


def client_meta(request: Request) -> tuple[str | None, str | None]:
    """Return (User-Agent, client IP): the first X-Forwarded-For value, else the socket peer."""
    user_agent = request.headers.get("user-agent")
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else None)
    return user_agent, ip


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    req: RegisterRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """Create an organization and its first admin; return tokens."""
    if not get_settings().allow_public_signup:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="public signup is disabled")
    user = await service.register_tenant(session, req)
    user_agent, ip = client_meta(request)
    return await tokens.build_token_response(session, user, user_agent=user_agent, ip=ip)


@router.post("/login")
async def login(
    req: LoginRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """Check credentials and return tokens (new refresh-token family)."""
    user = await service.authenticate(session, req.email, req.password)
    user_agent, ip = client_meta(request)
    return await tokens.build_token_response(session, user, user_agent=user_agent, ip=ip)


@router.post("/refresh")
async def refresh(
    req: RefreshRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    """Rotate a refresh token: new access token and new refresh token in the same family."""
    user_agent, ip = client_meta(request)
    user, new_raw = await tokens.rotate_refresh_token(
        session, req.refresh_token, user_agent=user_agent, ip=ip
    )
    return tokens.token_response(user, new_raw)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(req: LogoutRequest, session: AsyncSession = Depends(get_session)) -> Response:
    """Revoke the given refresh token (idempotent)."""
    await tokens.revoke_refresh_token(session, req.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me")
async def me(
    principal: Principal = Depends(get_principal), session: AsyncSession = Depends(get_session)
) -> MeResponse:
    """Return the caller and their tenant."""
    user, tenant = await service.get_me(session, principal)
    return MeResponse(user=UserOut.model_validate(user), tenant=TenantOut.model_validate(tenant))


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password_route(
    req: ChangePasswordRequest,
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Change the caller's password; every refresh token of the user is revoked."""
    user, _ = await service.get_me(session, principal)
    await service.change_password(session, user.id, req.current_password, req.new_password)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
