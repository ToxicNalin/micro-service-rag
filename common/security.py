"""JWT verification and the FastAPI dependencies for authentication, roles and internal calls."""

import hmac
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio.to_thread
import jwt
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from common.logging import get_request_id
from common.settings import get_base_settings

Role = Literal["admin", "member"]
_ROLES: tuple[str, ...] = ("admin", "member")
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "iss", "aud", "jti"]


@dataclass(frozen=True)
class Principal:
    """The verified caller of a request."""

    user_id: str
    tenant_id: str
    role: Role
    email: str
    token: str = field(repr=False)  # raw access token, kept out of repr so it never lands in logs

    @property
    def is_admin(self) -> bool:
        """True if the caller has the admin role."""
        return self.role == "admin"


class AuthError(Exception):
    """Raised for any token problem; the message is safe to show to users."""


class JWTVerifier:
    """Verifies RS256 access tokens against a JWKS endpoint or a static public key."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        public_key: Any | None = None,
    ) -> None:
        """Configure the verifier; pass exactly one of `jwks_url` or `public_key`."""
        if (jwks_url is None) == (public_key is None):
            raise ValueError("pass exactly one of jwks_url or public_key")
        self.issuer = issuer
        self.audience = audience
        self._public_key = public_key
        self._jwks_client = (
            jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300) if jwks_url is not None else None
        )

    @property
    def jwks_client(self) -> jwt.PyJWKClient | None:
        """The JWKS client in JWKS mode, else None (used to warm the key cache)."""
        return self._jwks_client

    def _signing_key(self, token: str) -> Any:
        if self._jwks_client is not None:
            return self._jwks_client.get_signing_key_from_jwt(token).key
        return self._public_key

    def verify(self, token: str) -> Principal:
        """Verify the token's signature and claims and return the Principal; raise AuthError otherwise."""
        try:
            claims = jwt.decode(
                token,
                self._signing_key(token),
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=10,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("token expired") from None
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AuthError("invalid token") from None

        tenant_id = claims.get("tid")
        role = claims.get("role")
        sub = claims.get("sub")
        if (
            claims.get("type") != "access"
            or not isinstance(tenant_id, str)
            or not tenant_id
            or role not in _ROLES
            or not isinstance(sub, str)
            or not sub
        ):
            raise AuthError("invalid token")
        email = claims.get("email")
        return Principal(
            user_id=sub,
            tenant_id=tenant_id,
            role=role,
            email=email if isinstance(email, str) else "",
            token=token,
        )


_verifier: JWTVerifier | None = None
_verifier_lock = threading.Lock()


def get_verifier() -> JWTVerifier:
    """Return the module verifier, building it lazily in JWKS mode from the base settings."""
    global _verifier
    if _verifier is None:
        with _verifier_lock:
            if _verifier is None:
                s = get_base_settings()
                _verifier = JWTVerifier(
                    issuer=s.jwt_issuer, audience=s.jwt_audience, jwks_url=s.auth_jwks_url
                )
    return _verifier


def set_verifier(verifier: JWTVerifier | None) -> None:
    """Replace the module verifier (auth service and tests); None resets it to lazy JWKS mode."""
    global _verifier
    with _verifier_lock:
        _verifier = verifier


bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> Principal:
    """FastAPI dependency: verify the Bearer token and return the caller; 401 if missing or invalid."""
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _unauthorized("not authenticated")
    verifier = get_verifier()
    try:
        # The first JWKS fetch is blocking network I/O, so verify in a worker thread.
        principal = await anyio.to_thread.run_sync(verifier.verify, credentials.credentials)
    except AuthError as exc:
        raise _unauthorized(str(exc)) from None
    request.state.principal = principal
    return principal


def require_roles(*roles: Role) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that returns the Principal, or raises 403 if its role is not allowed."""
    allowed = frozenset(roles)

    async def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return principal

    return dependency


require_admin = require_roles("admin")


async def require_internal_token(x_internal_token: str | None = Header(None)) -> None:
    """FastAPI dependency for /internal/* routes: 403 unless X-Internal-Token matches."""
    expected = get_base_settings().internal_api_token
    if x_internal_token is None or not hmac.compare_digest(
        x_internal_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def internal_headers() -> dict[str, str]:
    """Headers for a service-to-service call made without a user."""
    return {"X-Internal-Token": get_base_settings().internal_api_token, "X-Request-ID": get_request_id()}


def forward_headers(principal: Principal) -> dict[str, str]:
    """Headers for a call made on behalf of a user: forwards their access token."""
    return {"Authorization": f"Bearer {principal.token}", "X-Request-ID": get_request_id()}
