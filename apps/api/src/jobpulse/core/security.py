"""Authentication & authorization.

The Next.js app (Auth.js) mints a short-lived Ed25519-signed (EdDSA) JWT for each backend
call, server-side only. The API holds only public keys, so it can verify but never forge
tokens. It checks signature (by ``kid``), algorithm, issuer, audience, expiry, not-before
and token id.

The token says only *who* the caller is (``sub`` = ``<provider>:<immutable subject>``) and,
optionally, which workspace they selected (``wid``). *What* they may do is decided by the
API from its own database: workspace roles come from memberships (see jobpulse.api.tenancy),
and the ``OWNER_GITHUB_IDS`` allowlist marks platform admins. Role claims are ignored.

Anonymous callers (and signed anonymous visitors) are read-only viewers of the public demo
workspace when ``PUBLIC_DEMO_ENABLED`` is true.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, Request

from jobpulse.api.deps import Ctx
from jobpulse.core.config import Settings
from jobpulse.core.errors import AuthenticationError
from jobpulse.core.jwks import JWT_ALGORITHM, load_jwks

JWT_LEEWAY_SECONDS = 30
MAX_TOKEN_LENGTH = 4096
MAX_TOKEN_LIFETIME_SECONDS = 15 * 60
# Immutable provider subjects only (never logins or emails, which can change hands):
# GitHub numeric ids, OIDC "sub" values, and SHA-256 hashes of verified email addresses.
SUBJECT_PATTERNS = {
    "github": re.compile(r"^[1-9][0-9]{0,19}$"),
    "google": re.compile(r"^[A-Za-z0-9_-]{1,255}$"),
    "microsoft": re.compile(r"^[A-Za-z0-9_-]{1,255}$"),
    "email": re.compile(r"^[0-9a-f]{64}$"),
}
# Anonymous visitors: keyed hash of their IP, signed by the web tier. Read-only, but each
# visitor gets their own rate-limit bucket instead of sharing the web server's IP.
VISITOR_RE = re.compile(r"^visitor:[0-9a-f]{32}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    authenticated: bool
    login: str | None = None
    provider: str | None = None
    provider_subject: str | None = None
    platform_admin: bool = False
    workspace_hint: uuid.UUID | None = None

    @property
    def actor(self) -> str:
        if not self.authenticated:
            return "anonymous"
        return f"{self.subject} ({self.login})" if self.login else self.subject


ANONYMOUS = Principal(subject="anonymous", authenticated=False)


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
    subject = str(claims["sub"])
    if VISITOR_RE.fullmatch(subject):
        return Principal(subject=subject, authenticated=False)
    provider, _, provider_subject = subject.partition(":")
    pattern = SUBJECT_PATTERNS.get(provider)
    if pattern is None or not pattern.fullmatch(provider_subject):
        raise AuthenticationError("invalid subject")
    login = claims.get("login")
    login = login if isinstance(login, str) and LOGIN_RE.fullmatch(login) else None
    return Principal(
        subject=subject,
        authenticated=True,
        login=login,
        provider=provider,
        provider_subject=provider_subject,
        platform_admin=provider == "github" and int(provider_subject) in settings.owner_github_ids,
        workspace_hint=_workspace_claim(claims.get("wid")),
    )


def _workspace_claim(value: object) -> uuid.UUID | None:
    """The selected workspace is only a hint: membership is verified against the database."""
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise AuthenticationError("invalid workspace claim") from exc


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
    if not principal.authenticated and not settings.public_demo_enabled:
        # Signed visitor tokens identify anonymous callers; they are not a login.
        raise AuthenticationError("authentication required")
    request.state.principal = principal
    return principal


Reader = Annotated[Principal, Depends(current_principal)]
