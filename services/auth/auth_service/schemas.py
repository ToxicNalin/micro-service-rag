"""Pydantic request and response models. Request models forbid extra fields (a stray tenant_id -> 422, I1)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from auth_service.models import UserRole
from auth_service.passwords import MAX_PASSWORD_LENGTH

_Password = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_Request):
    """Sign up a new organization and its first (admin) user."""

    tenant_name: str = Field(min_length=2, max_length=120)
    full_name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = _Password


class LoginRequest(_Request):
    """Email and password login."""

    email: EmailStr
    password: str = _Password


class RefreshRequest(_Request):
    """A raw refresh token to rotate."""

    refresh_token: str = Field(min_length=1, max_length=512)


class LogoutRequest(_Request):
    """A raw refresh token to revoke."""

    refresh_token: str = Field(min_length=1, max_length=512)


class TokenResponse(BaseModel):
    """Access token plus the one-time raw refresh token."""

    access_token: str
    token_type: str = "bearer"  # noqa: S105 - the OAuth token type, not a secret
    expires_in: int
    refresh_token: str
    refresh_expires_in: int


class TenantOut(BaseModel):
    """Public view of a tenant."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str


class UserOut(BaseModel):
    """Public view of a user (never the password hash)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class MeResponse(BaseModel):
    """The caller and their tenant."""

    user: UserOut
    tenant: TenantOut


class CreateUserRequest(_Request):
    """An admin creates a user in their own tenant."""

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    password: str = _Password
    role: UserRole = UserRole.member


class UpdateUserRequest(_Request):
    """Partial update of a user; omitted fields are left unchanged."""

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: UserRole | None = None
    is_active: bool | None = None


class ChangePasswordRequest(_Request):
    """Change the caller's own password."""

    current_password: str = _Password
    new_password: str = _Password
