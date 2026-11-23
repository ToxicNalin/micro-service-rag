"""Auth service settings."""

from functools import lru_cache

from common.settings import BaseServiceSettings


class Settings(BaseServiceSettings):
    """Settings for the auth service, read from environment variables."""

    database_url: str
    jwt_private_key_path: str = "/keys/jwt_private.pem"
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 7
    allow_public_signup: bool = True
    password_min_length: int = 10
    login_max_failed_attempts: int = 5
    login_lockout_minutes: int = 15


@lru_cache
def get_settings() -> Settings:
    """Return the cached auth settings."""
    return Settings()
