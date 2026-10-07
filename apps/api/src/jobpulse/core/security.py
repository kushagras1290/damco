"""Authentication & authorization.

The Next.js app (Auth.js + GitHub OAuth) mints a short-lived Ed25519-signed (EdDSA)
JWT for each backend call, server-side only. The API holds only public keys, so it can
verify but never forge tokens. It checks signature (by ``kid``), algorithm, issuer,
audience, expiry, not-before and token id, then **derives the role itself** from its own
``OWNER_GITHUB_IDS`` allowlist using the immutable numeric GitHub user id in ``sub``.
Any role claim sent by the web app is ignored: authorization is decided server-side.

Anonymous callers are PUBLIC_DEMO (read-only) when ``PUBLIC_DEMO_ENABLED`` is true.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import jwt
from fastapi import Depends, Request

from jobpulse.api.deps import Ctx
from jobpulse.core.config import Settings
from jobpulse.core.errors import AuthenticationError, PermissionDeniedError
from jobpulse.core.jwks import JWT_ALGORITHM, load_jwks

JWT_LEEWAY_SECONDS = 30
MAX_TOKEN_LENGTH = 4096
MAX_TOKEN_LIFETIME_SECONDS = 15 * 60
SUBJECT_RE = re.compile(r"^github:(?P<id>[1-9][0-9]{0,19})$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


class Role(StrEnum):
    PUBLIC_DEMO = "PUBLIC_DEMO"
    OWNER = "OWNER"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    role: Role
    authenticated: bool
    login: str | None = None

    @property
    def actor(self) -> str:
        if not self.authenticated:
            return "anonymous"
        return f"{self.subject} ({self.login})" if self.login else self.subject


ANONYMOUS = Principal(subject="anonymous", role=Role.PUBLIC_DEMO, authenticated=False)


def decode_token(token: str, settings: Settings) -> Principal:
    if len(token) > MAX_TOKEN_LENGTH:
        raise AuthenticationError("token too long")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid token") from exc
    # Reject before key lookup: blocks alg-confusion (e.g. HS256 signed with a public key).
    if header.get("alg") != JWT_ALGORITHM:
        raise AuthenticationError("invalid token algorithm")
    kid = header.get("kid")
    key = load_jwks(settings.api_jwt_jwks).get(kid) if isinstance(kid, str) else None
    if key is None:
        raise AuthenticationError("unknown signing key")
    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=[JWT_ALGORITHM],
            audience=settings.api_jwt_audience,
            issuer=settings.api_jwt_issuer,
            leeway=JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "nbf", "sub", "aud", "iss", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid token") from exc

    if int(claims["exp"]) - int(claims["iat"]) > MAX_TOKEN_LIFETIME_SECONDS:
        raise AuthenticationError("token lifetime too long")
    match = SUBJECT_RE.fullmatch(str(claims["sub"]))
    if match is None:
        raise AuthenticationError("invalid subject")
    github_id = int(match.group("id"))
    login = claims.get("login")
    login = login if isinstance(login, str) and LOGIN_RE.fullmatch(login) else None
    role = Role.OWNER if github_id in settings.owner_github_ids else Role.PUBLIC_DEMO
    return Principal(subject=str(claims["sub"]), role=role, authenticated=True, login=login)


async def current_principal(request: Request, ctx: Ctx) -> Principal:
    settings = ctx.settings
    header = request.headers.get("authorization")
    if not header:
        if not settings.public_demo_enabled:
            raise AuthenticationError("authentication required")
        return ANONYMOUS
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("malformed authorization header")
    principal = decode_token(token.strip(), settings)
    request.state.principal = principal
    return principal


async def require_owner(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.role is not Role.OWNER:
        raise PermissionDeniedError("owner role required")
    return principal


Reader = Annotated[Principal, Depends(current_principal)]
Owner = Annotated[Principal, Depends(require_owner)]
