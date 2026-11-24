"""Auth business logic. Raises ServiceError, which main.py turns into a JSON error response."""

import logging
import re
import unicodedata
import uuid
from datetime import timedelta

import anyio.to_thread
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service.config import get_settings
from auth_service.models import Tenant, User, UserRole, as_utc, utcnow
from auth_service.passwords import (
    hash_password,
    validate_password_strength,
    verify_dummy,
    verify_password,
)
from auth_service.schemas import CreateUserRequest, RegisterRequest, UpdateUserRequest
from auth_service.tokens import revoke_all_user_tokens
from common.security import Principal

log = logging.getLogger(__name__)


class ServiceError(Exception):
    """A business-rule failure with an HTTP status code and a user-safe message."""

    def __init__(self, status_code: int, detail: str) -> None:
        """Store the status code and message."""
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def normalize_email(email: str) -> str:
    """Strip and lower-case an email address."""
    return email.strip().lower()


def slugify(name: str) -> str:
    """Lower-case ASCII words joined by '-', at most 48 characters; 'org' if nothing is left."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:48].strip("-") or "org"


async def unique_tenant_slug(session: AsyncSession, name: str) -> str:
    """slugify(name), then name-2, name-3, ... until the slug is unused."""
    base = slugify(name)
    candidate, n = base, 1
    while await session.scalar(select(exists().where(Tenant.slug == candidate))):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


async def _email_taken(session: AsyncSession, email: str) -> bool:
    return bool(await session.scalar(select(exists().where(User.email == email))))


def _check_strength(password: str, email: str) -> None:
    try:
        validate_password_strength(password, email=email, min_length=get_settings().password_min_length)
    except ValueError as exc:
        raise ServiceError(400, str(exc)) from None


async def register_tenant(session: AsyncSession, req: RegisterRequest) -> User:
    """Create a tenant and its first user (role admin) in one transaction."""
    email = normalize_email(req.email)
    _check_strength(req.password, email)
    if await _email_taken(session, email):
        raise ServiceError(409, "email already registered")
    password_hash = await anyio.to_thread.run_sync(hash_password, req.password)
    slug = await unique_tenant_slug(session, req.tenant_name)
    tenant = Tenant(id=uuid.uuid4(), name=req.tenant_name.strip(), slug=slug)
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=email,
        full_name=req.full_name.strip(),
        password_hash=password_hash,
        role=UserRole.admin,
    )
    session.add_all([tenant, user])
    try:
        await session.commit()
    except IntegrityError:
        # a concurrent signup took the email or the slug between our checks and the insert
        await session.rollback()
        if await _email_taken(session, email):
            raise ServiceError(409, "email already registered") from None
        raise ServiceError(409, "organization name is busy, please try again") from None
    await session.refresh(user, attribute_names=["tenant"])
    log.info("tenant registered", extra={"tenant_id": str(tenant.id), "user_id": str(user.id)})
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Check credentials with lockout after repeated failures; return the user on success."""
    s = get_settings()
    user = (
        (await session.execute(select(User).where(User.email == normalize_email(email))))
        .unique()
        .scalar_one_or_none()
    )
    if user is None:
        await anyio.to_thread.run_sync(verify_dummy, password)
        raise ServiceError(401, "invalid email or password")
    now = utcnow()
    if user.locked_until is not None and as_utc(user.locked_until) > now:
        raise ServiceError(423, "account locked, try again later")
    ok, new_hash = await anyio.to_thread.run_sync(verify_password, password, user.password_hash)
    if not ok:
        user.failed_login_count += 1
        if user.failed_login_count >= s.login_max_failed_attempts:
            user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
            user.failed_login_count = 0
            log.warning("account locked after failed logins", extra={"user_id": str(user.id)})
        await session.commit()
        raise ServiceError(401, "invalid email or password")
    if not user.is_active or not user.tenant.is_active:
        raise ServiceError(403, "account disabled")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if new_hash:
        user.password_hash = new_hash
    await session.commit()
    return user


def _uuid_or_404(value: str | uuid.UUID, what: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        raise ServiceError(404, f"{what} not found") from None


async def change_password(session: AsyncSession, user_id: uuid.UUID, current: str, new: str) -> None:
    """Verify the current password, store the new one and revoke all refresh tokens."""
    user = await session.get(User, _uuid_or_404(user_id, "user"))
    if user is None:
        raise ServiceError(404, "user not found")
    ok, _ = await anyio.to_thread.run_sync(verify_password, current, user.password_hash)
    if not ok:
        raise ServiceError(400, "current password is incorrect")
    _check_strength(new, user.email)
    user.password_hash = await anyio.to_thread.run_sync(hash_password, new)
    revoked = await revoke_all_user_tokens(session, user.id)
    await session.commit()
    log.info("password changed", extra={"user_id": str(user.id), "revoked_tokens": revoked})


async def get_me(session: AsyncSession, principal: Principal) -> tuple[User, Tenant]:
    """Load the caller and their tenant (404 if either is gone)."""
    stmt = select(User).where(
        User.id == _uuid_or_404(principal.user_id, "user"),
        User.tenant_id == _uuid_or_404(principal.tenant_id, "user"),
    )
    user = (await session.execute(stmt)).unique().scalar_one_or_none()
    if user is None:
        raise ServiceError(404, "user not found")
    return user, user.tenant


# ---------- user management (admins, own tenant only) ----------

LAST_ADMIN_MESSAGE = "a tenant needs at least one active admin"


async def get_user_in_tenant(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """Return the user only if it belongs to this tenant; otherwise 404, never 403 (I4)."""
    stmt = select(User).where(
        User.id == _uuid_or_404(user_id, "user"), User.tenant_id == _uuid_or_404(tenant_id, "user")
    )
    user = (await session.execute(stmt)).unique().scalar_one_or_none()
    if user is None:
        raise ServiceError(404, "user not found")
    return user


async def list_users(session: AsyncSession, tenant_id: uuid.UUID) -> list[User]:
    """All users of one tenant, oldest first."""
    stmt = (
        select(User)
        .where(User.tenant_id == _uuid_or_404(tenant_id, "tenant"))
        .order_by(User.created_at, User.email)
    )
    return list((await session.execute(stmt)).unique().scalars().all())


async def create_user(session: AsyncSession, tenant_id: uuid.UUID, req: CreateUserRequest) -> User:
    """Create a user in the tenant; 409 on a duplicate email, 400 for a weak password."""
    email = normalize_email(req.email)
    _check_strength(req.password, email)
    if await _email_taken(session, email):
        raise ServiceError(409, "email already registered")
    user = User(
        id=uuid.uuid4(),
        tenant_id=_uuid_or_404(tenant_id, "tenant"),
        email=email,
        full_name=req.full_name.strip(),
        password_hash=await anyio.to_thread.run_sync(hash_password, req.password),
        role=req.role,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "email already registered") from None
    log.info("user created", extra={"tenant_id": str(user.tenant_id), "user_id": str(user.id)})
    return await get_user_in_tenant(session, user.tenant_id, user.id)


async def count_active_admins(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """Count the tenant's active admins, locking those rows so concurrent demotions serialize."""
    stmt = (
        select(User.id)
        .where(
            User.tenant_id == _uuid_or_404(tenant_id, "tenant"),
            User.role == UserRole.admin,
            User.is_active.is_(True),
        )
        .with_for_update()
    )
    return len((await session.execute(stmt)).scalars().all())


async def _guard_last_admin(
    session: AsyncSession, user: User, *, new_role: UserRole, new_active: bool
) -> None:
    """409 if this change would leave the tenant without an active admin."""
    was_active_admin = user.role == UserRole.admin and user.is_active
    stays_active_admin = new_role == UserRole.admin and new_active
    if (
        was_active_admin
        and not stays_active_admin
        and await count_active_admins(session, user.tenant_id) <= 1
    ):
        raise ServiceError(409, LAST_ADMIN_MESSAGE)


async def update_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    req: UpdateUserRequest,
    acting: Principal,
) -> User:
    """Apply a partial update; protects the last admin; deactivation revokes the user's tokens."""
    user = await get_user_in_tenant(session, tenant_id, user_id)
    new_role = req.role if req.role is not None else user.role
    new_active = req.is_active if req.is_active is not None else user.is_active
    await _guard_last_admin(session, user, new_role=new_role, new_active=new_active)
    if not new_active and str(user.id) == acting.user_id:
        raise ServiceError(400, "you cannot deactivate your own account")

    deactivating = user.is_active and not new_active
    if req.full_name is not None:
        user.full_name = req.full_name.strip()
    user.role = new_role
    user.is_active = new_active
    if deactivating:
        await revoke_all_user_tokens(session, user.id)
    await session.commit()
    log.info(
        "user updated",
        extra={"tenant_id": str(user.tenant_id), "user_id": str(user.id), "by": acting.user_id},
    )
    return await get_user_in_tenant(session, user.tenant_id, user.id)


async def deactivate_user(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, acting: Principal
) -> None:
    """Deactivate a user (never yourself, never the last admin) and revoke their refresh tokens."""
    user = await get_user_in_tenant(session, tenant_id, user_id)
    await _guard_last_admin(session, user, new_role=user.role, new_active=False)
    if str(user.id) == acting.user_id:
        raise ServiceError(400, "you cannot deactivate your own account")
    user.is_active = False
    revoked = await revoke_all_user_tokens(session, user.id)
    await session.commit()
    log.info(
        "user deactivated",
        extra={
            "tenant_id": str(user.tenant_id),
            "user_id": str(user.id),
            "by": acting.user_id,
            "revoked_tokens": revoked,
        },
    )
