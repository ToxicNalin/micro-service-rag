"""Argon2id password hashing and password-strength rules. These calls are CPU-heavy: run them in a thread."""

import re
from functools import lru_cache

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

MAX_PASSWORD_LENGTH = 256  # bounds hashing cost for absurd inputs


@lru_cache
def _hasher() -> PasswordHash:
    return PasswordHash.recommended()  # Argon2id


@lru_cache
def _dummy_hash() -> str:
    return _hasher().hash("dummy-password-for-timing-only-1")


def hash_password(plain: str) -> str:
    """Return an Argon2id hash of the password."""
    return _hasher().hash(plain)


def verify_password(plain: str, hashed: str) -> tuple[bool, str | None]:
    """Check a password; return (ok, new hash if the hashing parameters changed, else None)."""
    try:
        return _hasher().verify_and_update(plain, hashed)
    except UnknownHashError:
        return False, None


def verify_dummy(plain: str) -> None:
    """Spend the same time as a real verification, so unknown emails can't be found by timing."""
    _hasher().verify(plain, _dummy_hash())


def validate_password_strength(password: str, *, email: str, min_length: int) -> None:
    """Raise ValueError with a user-friendly message if the password is too weak."""
    if len(password) < min_length:
        raise ValueError(f"Password must be at least {min_length} characters long.")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.")
    if not re.search(r"[^\W\d_]", password):
        raise ValueError("Password must contain at least one letter.")
    if not re.search(r"\d", password):
        raise ValueError("Password must contain at least one digit.")
    local_part = email.split("@", 1)[0].strip().lower()
    if len(local_part) >= 3 and local_part in password.lower():
        raise ValueError("Password must not contain your email address.")
