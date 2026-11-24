"""The RSA signing key (created once in the jwt_keys volume) and the public JWKS."""

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm

log = logging.getLogger(__name__)

_state: dict[str, Any] = {}  # private_key, public_key, kid (filled by init_keys)


def load_or_create_private_key(path: str) -> RSAPrivateKey:
    """Load the PEM key at `path`; if missing, generate RSA-2048 and save it as PKCS8 PEM (mode 600)."""
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        try:
            # O_EXCL: if another process created the file first, we load theirs instead
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            try:
                os.chmod(p, 0o600)
            except OSError:  # not supported everywhere (e.g. some Windows setups)
                pass
            log.info("created new JWT signing key", extra={"key_path": str(p)})
    loaded = serialization.load_pem_private_key(p.read_bytes(), password=None)
    if not isinstance(loaded, RSAPrivateKey):
        raise ValueError(f"{path} is not an RSA private key")
    return loaded


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8 or 1, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def key_id(public_key: RSAPublicKey) -> str:
    """RFC 7638 JWK thumbprint: base64url(sha256(compact sorted JSON of e, kty, n)), unpadded."""
    numbers = public_key.public_numbers()
    members = {"e": _b64url_uint(numbers.e), "kty": "RSA", "n": _b64url_uint(numbers.n)}
    digest = hashlib.sha256(json.dumps(members, separators=(",", ":"), sort_keys=True).encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def public_jwk(public_key: RSAPublicKey, kid: str) -> dict[str, Any]:
    """The public key as a JWK with kid, use=sig and alg=RS256 (no key_ops)."""
    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    jwk.pop("key_ops", None)
    jwk.update(kid=kid, use="sig", alg="RS256")
    return jwk


def init_keys(path: str) -> None:
    """Load or create the signing key and keep it, its public key and kid in memory."""
    private_key = load_or_create_private_key(str(path))
    public = private_key.public_key()
    _state.update(private_key=private_key, public_key=public, kid=key_id(public))


def _get(name: str) -> Any:
    if name not in _state:
        raise RuntimeError("signing keys are not initialised; call init_keys() first")
    return _state[name]


def signing_key() -> RSAPrivateKey:
    """The private signing key."""
    return _get("private_key")


def public_key() -> RSAPublicKey:
    """The public verification key."""
    return _get("public_key")


def current_kid() -> str:
    """The key ID (RFC 7638 thumbprint) put in every token header."""
    return _get("kid")


def jwks() -> dict[str, list[dict[str, Any]]]:
    """The public JSON Web Key Set served at /.well-known/jwks.json."""
    return {"keys": [public_jwk(public_key(), current_kid())]}
