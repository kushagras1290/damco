"""Test-only Ed25519 key material and token minting that mirrors apps/web/lib/backend-proxy.ts."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jobpulse.keys import generate

OWNER_ID = 583231  # numeric GitHub id of the test owner
VISITOR_ID = 9919
TEST_KID = "test-kid"

PRIVATE_JWK, JWKS = generate(TEST_KID)
JWKS_JSON = json.dumps(JWKS)
SIGNING_KEY = jwt.PyJWK(PRIVATE_JWK, algorithm="EdDSA").key


def make_token(
    github_id: int = OWNER_ID,
    login: str = "octocat",
    *,
    key: Any = None,
    kid: str = TEST_KID,
    algorithm: str = "EdDSA",
    lifetime: timedelta = timedelta(minutes=5),
    **overrides: Any,
) -> str:
    now = datetime.now(tz=UTC)
    claims: dict[str, Any] = {
        "sub": f"github:{github_id}",
        "login": login,
        "iss": "jobpulse-web",
        "aud": "jobpulse-api",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    claims.update(overrides)
    return jwt.encode(claims, key or SIGNING_KEY, algorithm=algorithm, headers={"kid": kid})


def other_signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()
