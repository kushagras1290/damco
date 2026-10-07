"""Canonical identity of a job board, used to share one poller between workspaces.

Two workspaces that add the same board (same ATS token, or the same feed/page URL modulo
cosmetic differences) follow the same catalogue source instead of polling it twice.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from jobpulse_core.domain.models import SourceDefinition, SourceKind

TOKEN_KINDS = frozenset({SourceKind.GREENHOUSE, SourceKind.LEVER, SourceKind.ASHBY})
DEFAULT_PORTS = {"http": 80, "https": 443}
DEMO_LOCATOR = "demo"


def normalize_url(url: str) -> str:
    """Lower-case scheme/host, drop default port, fragment and trailing slash; keep the query."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port is None or DEFAULT_PORTS.get(scheme) == port else f"{host}:{port}"
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def source_locator(definition: SourceDefinition) -> str:
    if definition.kind is SourceKind.DEMO:
        return DEMO_LOCATOR
    if definition.kind in TOKEN_KINDS:
        if not definition.board_token:
            msg = f"{definition.kind.value} sources require board_token"
            raise ValueError(msg)
        return definition.board_token.strip().lower()
    if definition.url is None:
        msg = f"{definition.kind.value} sources require url"
        raise ValueError(msg)
    return normalize_url(str(definition.url))
