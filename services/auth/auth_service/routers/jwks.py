"""Public signing keys for every service that verifies tokens."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from auth_service import keys

router = APIRouter(tags=["jwks"])


@router.get("/.well-known/jwks.json")
async def get_jwks() -> JSONResponse:
    """Return the public JWKS (cacheable for 5 minutes)."""
    return JSONResponse(keys.jwks(), headers={"Cache-Control": "public, max-age=300"})
