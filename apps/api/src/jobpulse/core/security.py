"""Authentication & authorization.

The Next.js app (Auth.js + GitHub OAuth) mints a short-lived HS256 JWT for each
backend call, server-side only. The API verifies signature, issuer, audience and
expiry, and derives the role from the token. Anonymous callers are PUBLIC_DEMO
(read-only) when ``PUBLIC_DEMO_ENABLED`` is true. Authorization is always enforced
here, never trusted from the browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

import jwt
from fastapi import Depends, Request

from jobpulse.api.deps import Ctx
from jobpulse.core.config import Settings
from jobpulse.core.errors import AuthenticationError, PermissionDeniedError

JWT_ALGORITHM = "HS256"
JWT_LEEWAY_SECONDS = 30
MAX_TOKEN_LENGTH = 4096


class Role(StrEnum):
    PUBLIC_DEMO = "PUBLIC_DEMO"
    OWNER = "OWNER"


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    role: Role
    authenticated: bool

    @property
    def actor(self) -> str:
        return f"github:{self.subject}" if self.authenticated else "anonymous"


ANONYMOUS = Principal(subject="anonymous", role=Role.PUBLIC_DEMO, authenticated=False)


def decode_token(token: str, settings: Settings) -> Principal:
    if len(token) > MAX_TOKEN_LENGTH:
        raise AuthenticationError("token too long")
    try:
        claims = jwt.decode(
            token,
            settings.api_jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            audience=settings.api_jwt_audience,
            issuer=settings.api_jwt_issuer,
            leeway=JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("invalid token") from exc
    raw_role = claims.get("role", Role.PUBLIC_DEMO.value)
    try:
        role = Role(raw_role)
    except ValueError as exc:
        raise AuthenticationError("invalid role claim") from exc
    subject = str(claims["sub"])
    return Principal(subject=subject, role=role, authenticated=True)


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
