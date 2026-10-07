"""Adapter factory."""

from __future__ import annotations

from urllib.parse import urlsplit

from jobpulse_core.domain.models import SourceDefinition, SourceKind
from jobpulse_core.ingestion.http import SafeHttpClient
from jobpulse_core.sources.ats import AshbySource, GreenhouseSource, LeverSource
from jobpulse_core.sources.base import BaseSource, JobSource
from jobpulse_core.sources.web import (
    DynamicHTMLSource,
    GenericJSONSource,
    RSSSource,
    StaticHTMLSource,
)

_ADAPTERS: dict[SourceKind, type[BaseSource]] = {
    SourceKind.GREENHOUSE: GreenhouseSource,
    SourceKind.LEVER: LeverSource,
    SourceKind.ASHBY: AshbySource,
    SourceKind.RSS: RSSSource,
    SourceKind.GENERIC_JSON: GenericJSONSource,
    SourceKind.STATIC_HTML: StaticHTMLSource,
    SourceKind.DYNAMIC_HTML: DynamicHTMLSource,
}

_ATS_HOSTS: dict[SourceKind, str] = {
    SourceKind.GREENHOUSE: "boards-api.greenhouse.io",
    SourceKind.LEVER: "api.lever.co",
    SourceKind.ASHBY: "api.ashbyhq.com",
}


def build_source(definition: SourceDefinition, http: SafeHttpClient) -> JobSource:
    adapter = _ADAPTERS[definition.kind](definition, http)
    if not isinstance(adapter, JobSource):  # pragma: no cover - structural guarantee
        msg = f"{type(adapter).__name__} does not satisfy JobSource"
        raise TypeError(msg)
    return adapter


def required_hosts(definition: SourceDefinition) -> list[str]:
    """Hosts that must be on the outbound allowlist for this source to work."""
    if definition.kind in _ATS_HOSTS:
        return [_ATS_HOSTS[definition.kind]]
    if definition.url is None:
        return []
    host = urlsplit(str(definition.url)).hostname
    return [host] if host else []
