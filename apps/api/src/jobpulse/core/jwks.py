"""Public-key set for verifying web-minted access tokens (Ed25519 / EdDSA).

The web app holds the private key and signs; the API holds only public keys and can
verify but never mint. Multiple keys (distinct ``kid``) allow zero-downtime rotation:
publish the new public key, switch the signer, then retire the old key.
"""

from __future__ import annotations

import json
from functools import lru_cache

import jwt
from jwt.exceptions import PyJWKError, PyJWKSetError

JWT_ALGORITHM = "EdDSA"
REQUIRED_KTY = "OKP"
REQUIRED_CRV = "Ed25519"
MAX_KEYS = 10


class JwksConfigError(ValueError):
    """API_JWT_JWKS is malformed or contains unsafe material."""


def validate_jwks(raw: str) -> dict[str, jwt.PyJWK]:
    """Parse and vet a JWKS document. Returns keys indexed by ``kid``."""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "API_JWT_JWKS is not valid JSON"
        raise JwksConfigError(msg) from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list) or not keys:
        msg = "API_JWT_JWKS must be a JWK Set with a non-empty 'keys' array"
        raise JwksConfigError(msg)
    if len(keys) > MAX_KEYS:
        msg = f"API_JWT_JWKS holds more than {MAX_KEYS} keys"
        raise JwksConfigError(msg)

    indexed: dict[str, jwt.PyJWK] = {}
    for key in keys:
        if not isinstance(key, dict):
            msg = "API_JWT_JWKS entries must be JSON objects"
            raise JwksConfigError(msg)
        if "d" in key:
            # A private key here would let the API mint tokens: refuse to start.
            msg = "API_JWT_JWKS must contain PUBLIC keys only (found private 'd' parameter)"
            raise JwksConfigError(msg)
        if key.get("kty") != REQUIRED_KTY or key.get("crv") != REQUIRED_CRV:
            msg = "API_JWT_JWKS keys must be Ed25519 (kty=OKP, crv=Ed25519)"
            raise JwksConfigError(msg)
        kid = key.get("kid")
        if not isinstance(kid, str) or not kid:
            msg = "every API_JWT_JWKS key needs a non-empty 'kid'"
            raise JwksConfigError(msg)
        if kid in indexed:
            msg = f"duplicate kid in API_JWT_JWKS: {kid}"
            raise JwksConfigError(msg)
        try:
            indexed[kid] = jwt.PyJWK(key, algorithm=JWT_ALGORITHM)
        except (PyJWKError, PyJWKSetError) as exc:
            msg = f"API_JWT_JWKS key {kid!r} is invalid"
            raise JwksConfigError(msg) from exc
    return indexed


@lru_cache(maxsize=4)
def load_jwks(raw: str) -> dict[str, jwt.PyJWK]:
    """Cached parse (settings are immutable for the process lifetime)."""
    return validate_jwks(raw)
