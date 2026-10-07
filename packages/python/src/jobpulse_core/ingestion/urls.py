"""Canonical URL normalisation used for layer-2 deduplication."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobpulse_core.errors import ValidationError

TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_", "mc_", "_hs", "hsa_", "pk_")
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "gclid",
        "fbclid",
        "msclkid",
        "ref",
        "referrer",
        "source",
        "src",
        "gh_src",
        "lever-source",
        "lever-origin",
        "trk",
        "trkid",
        "igshid",
        "yclid",
    },
)
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PARAM_PREFIXES)


def canonicalize_url(url: str) -> str:
    """Return a stable canonical form of ``url``.

    - lower-cases scheme and host, strips ``www.``
    - upgrades ``http`` to ``https`` (protocol aliases)
    - drops default ports, fragments, userinfo and tracking parameters
    - sorts remaining query parameters
    - removes trailing slashes (except root)
    """
    stripped = url.strip()
    parts = urlsplit(stripped)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        msg = f"unsupported URL scheme: {scheme!r}"
        raise ValidationError(msg, context={"url": stripped})
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        msg = "URL has no host"
        raise ValidationError(msg, context={"url": stripped})
    host = host.removeprefix("www.")

    port = parts.port
    netloc = host if port is None or port == DEFAULT_PORTS.get(scheme) else f"{host}:{port}"

    path = parts.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    if len(path) > 1:
        path = path.rstrip("/")

    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _is_tracking(k)]
    query = urlencode(sorted(query_pairs))

    return urlunsplit(("https", netloc, path, query, ""))


def registrable_domain(host_or_url: str) -> str:
    """Best-effort company domain from a host or URL (``jobs.acme.io`` -> ``acme.io``).

    Deliberately simple (last two labels; three for common two-level ccTLDs) to avoid
    a public-suffix dependency; the value is only a fingerprint input.
    """
    candidate = host_or_url.strip().lower()
    if "://" in candidate:
        candidate = urlsplit(candidate).hostname or ""
    candidate = candidate.rstrip(".").removeprefix("www.")
    labels = [label for label in candidate.split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    two_level = {"co.uk", "co.in", "com.au", "co.jp", "com.br", "co.nz", "org.uk", "ac.uk"}
    if ".".join(labels[-2:]) in two_level:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
