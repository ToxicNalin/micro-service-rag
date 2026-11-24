"""Access tokens (RS256 JWTs) and rotating refresh tokens with reuse detection."""

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta

import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service import keys
from auth_service.config import get_settings
from auth_service.models import RefreshToken, User, as_utc, utcnow
from auth_service.schemas import TokenResponse
from common.security import AuthError

log = logging.getLogger(__name__)


def create_access_token(user: User, *, now: datetime | None = None) -> tuple[str, int]:
    """Sign an RS256 access token for the user; return (token, ttl in seconds)."""
    s = get_settings()
    ttl = int(s.access_token_ttl_min) * 60
    iat = int((now or utcnow()).timestamp())
    claims = {
        "iss": s.jwt_issuer,
        "aud": s.jwt_audience,
        "sub": str(user.id),
        "tid": str(user.tenant_id),
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "email": user.email,
        "iat": iat,
        "nbf": iat,
        "exp": iat + ttl,
        "jti": uuid.uuid4().hex,
        "type": "access",
    }
    token = jwt.encode(claims, keys.signing_key(), algorithm="RS256", headers={"kid": keys.current_kid()})
    return token, ttl


def generate_refresh_token() -> str:
    """Return a new random raw refresh token."""
    return secrets.token_urlsafe(48)


def hash_token(raw: str) -> str:
    """SHA-256 hex of a raw token; only this hash is ever stored."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def issue_refresh_token(
    session: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, RefreshToken]:
    """Create a refresh-token row (a new family if family_id is None); return the raw token and the row."""
    raw = generate_refresh_token()
    row = RefreshToken(
        id=uuid.uuid4(),
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=hash_token(raw),
        expires_at=utcnow() + timedelta(days=get_settings().refresh_token_ttl_days),
        user_agent=(user_agent or None) and user_agent[:255],
        ip_address=(ip or None) and ip[:45],
    )
    session.add(row)
    await session.flush()
    return raw, row


async def rotate_refresh_token(
    session: AsyncSession, raw: str, *, user_agent: str | None, ip: str | None
) -> tuple[User, str]:
    """Swap a valid refresh token for a new one in the same family; a reused token revokes the family."""
    stmt = select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw))
    row = (await session.execute(stmt.with_for_update(of=RefreshToken))).unique().scalar_one_or_none()
    if row is None:
        raise AuthError("invalid refresh token")
    now = utcnow()
    if row.revoked_at is not None:
        revoked = await revoke_family(session, row.family_id)
        await session.commit()
        log.warning(
            "refresh token reuse detected", extra={"family_id": str(row.family_id), "revoked": revoked}
        )
        raise AuthError("refresh token reuse detected")
    if as_utc(row.expires_at) <= now:
        raise AuthError("refresh token expired")
    user = row.user
    if not user.is_active or not user.tenant.is_active:
        raise AuthError("account disabled")
    new_raw, new_row = await issue_refresh_token(
        session, user, family_id=row.family_id, user_agent=user_agent, ip=ip
    )
    row.revoked_at = now
    row.replaced_by = new_row.id
    await session.commit()
    return user, new_raw


async def revoke_refresh_token(session: AsyncSession, raw: str) -> None:
    """Revoke a refresh token if it is active; unknown or already revoked tokens are ignored."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await session.commit()


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> int:
    """Revoke every active token of one login family; return how many were revoked (caller commits)."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)


async def revoke_all_user_tokens(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke all of a user's active refresh tokens; return the count (caller commits)."""
    result = await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)


async def build_token_response(
    session: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    user_agent: str | None = None,
    ip: str | None = None,
) -> TokenResponse:
    """Issue an access token and a new refresh token (committed) in one response."""
    raw, _ = await issue_refresh_token(session, user, family_id=family_id, user_agent=user_agent, ip=ip)
    await session.commit()
    return token_response(user, raw)


def token_response(user: User, raw_refresh: str) -> TokenResponse:
    """Build a TokenResponse from a fresh access token and an already-issued raw refresh token."""
    access, ttl = create_access_token(user)
    return TokenResponse(
        access_token=access,
        expires_in=ttl,
        refresh_token=raw_refresh,
        refresh_expires_in=get_settings().refresh_token_ttl_days * 86400,
    )
