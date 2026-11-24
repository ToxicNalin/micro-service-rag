"""ORM models for auth_db (PROJECT_SPEC §4.1). Portable types only, so tests can run on SQLite."""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import CHAR, DateTime, ForeignKey, Integer, String, Uuid, func, true
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from auth_service.db import Base


class UserRole(enum.StrEnum):
    """Roles a user can have within a tenant."""

    admin = "admin"
    member = "member"


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Treat a naive datetime as UTC (SQLite drops tzinfo); convert aware ones to UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Tenant(Base):
    """An organization; owns users."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    users: Mapped[list["User"]] = relationship(back_populates="tenant", passive_deletes=True)


class User(Base):
    """A person who can log in; belongs to exactly one tenant."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)  # stored lower-case
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # Argon2id
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=UserRole.member,
        server_default=UserRole.member.value,
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=true())
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users", lazy="joined", innerjoin=True)

    def __repr__(self) -> str:
        """Short repr that never includes the password hash."""
        return f"User(id={self.id}, tenant_id={self.tenant_id}, role={self.role})"


class RefreshToken(Base):
    """A refresh token, stored only as its SHA-256 hash; rotations of one login share a family_id."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    user: Mapped[User] = relationship(lazy="joined", innerjoin=True)

    def __repr__(self) -> str:
        """Short repr that never includes the token hash."""
        return f"RefreshToken(id={self.id}, user_id={self.user_id}, family_id={self.family_id})"


__all__ = ["Base", "RefreshToken", "Tenant", "User", "UserRole", "as_utc", "utcnow"]
