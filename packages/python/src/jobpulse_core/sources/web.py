"""Adapters for generic feeds and web pages: RSS/Atom, arbitrary JSON, static & dynamic HTML."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import feedparser
import structlog
from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode as Node

from jobpulse_core.domain.models import RawJob, SourceCheckpoint, SourceDefinition
from jobpulse_core.errors import (
    ConfigurationError,
    SourceFetchError,
    SourceParseError,
    UnsafeUrlError,
    ValidationError,
)
from jobpulse_core.ingestion.http import FetchResult, SafeHttpClient
from jobpulse_core.ingestion.normalize import sha256_hex
from jobpulse_core.ingestion.urls import canonicalize_url
from jobpulse_core.sources.base import BaseSource, DiscoveryResult, dig, parse_datetime

logger = structlog.get_logger(__name__)

PLAYWRIGHT_NAVIGATION_TIMEOUT_MS = 30_000
PLAYWRIGHT_MAX_HTML_CHARS = 5_000_000


def _require_url(definition: SourceDefinition) -> str:
    if definition.url is None:
        raise ConfigurationError("source url is required", context={"kind": definition.kind.value})
    return str(definition.url)


def _struct_time_to_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)


class RSSSource(BaseSource):
    """RSS 2.0 / Atom job feeds (e.g. WeWorkRemotely, company blogs)."""

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = _require_url(self.definition)
        result = await self._conditional_get(url, checkpoint)
        if result.not_modified:
            return self._not_modified(checkpoint, result)
        feed = feedparser.parse(result.content)
        if feed.get("bozo") and not feed.get("entries"):
            raise SourceParseError("feed could not be parsed", context={"url": url})

        skipped: list[dict[str, str]] = []
        jobs: list[RawJob] = []
        for entry in feed.get("entries", []):
            link = entry.get("link") or ""
            external_id = entry.get("id") or entry.get("guid") or link
            content_blocks = entry.get("content") or []
            description = content_blocks[0].get("value") if content_blocks else entry.get("summary")
            job = self._build(
                skipped,
                external_id=str(external_id)[:255],
                title=entry.get("title") or "",
                url=link,
                location=entry.get("location") or entry.get("region"),
                description_html=description,
                published_at=_struct_time_to_dt(entry.get("published_parsed") or entry.get("updated_parsed")),
                raw={"id": external_id, "link": link, "title": entry.get("title")},
            )
            if job is not None:
                jobs.append(job)
        return self._finish(checkpoint, result, jobs, skipped)


class GenericJSONSource(BaseSource):
    """Any JSON endpoint described by ``field_map`` dotted paths.

    Required keys: ``items``, ``external_id``, ``title``, ``url``.
    Optional keys: ``location``, ``description_html``, ``description_text``,
    ``published_at``, ``department``, ``employment_type``, ``remote``.
    """

    REQUIRED_KEYS: frozenset[str] = frozenset({"items", "external_id", "title", "url"})

    def __init__(self, definition: SourceDefinition, http: SafeHttpClient) -> None:
        super().__init__(definition, http)
        missing = self.REQUIRED_KEYS - definition.field_map.keys()
        if missing:
            raise ConfigurationError("generic_json field_map incomplete", context={"missing": sorted(missing)})

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = _require_url(self.definition)
        fmap = self.definition.field_map
        result = await self._conditional_get(url, checkpoint)
        if result.not_modified:
            return self._not_modified(checkpoint, result)
        payload = self._decode_json(result)
        items = dig(payload, fmap["items"])
        if not isinstance(items, list):
            raise SourceParseError("items path did not resolve to a list", context={"path": fmap["items"]})

        skipped: list[dict[str, str]] = []
        jobs: list[RawJob] = []
        for item in items:
            remote_value = dig(item, fmap["remote"]) if "remote" in fmap else None
            job_url = str(dig(item, fmap["url"]) or "")
            job = self._build(
                skipped,
                external_id=str(dig(item, fmap["external_id"]) or ""),
                title=str(dig(item, fmap["title"]) or ""),
                url=urljoin(url, job_url) if job_url else "",
                location=_opt_str(item, fmap.get("location")),
                department=_opt_str(item, fmap.get("department")),
                employment_type=_opt_str(item, fmap.get("employment_type")),
                description_html=_opt_str(item, fmap.get("description_html")),
                description_text=_opt_str(item, fmap.get("description_text")),
                published_at=parse_datetime(dig(item, fmap["published_at"])) if "published_at" in fmap else None,
                remote_hint=remote_value if isinstance(remote_value, bool) else None,
                raw=item if isinstance(item, dict) else {"value": item},
            )
            if job is not None:
                jobs.append(job)
        return self._finish(checkpoint, result, jobs, skipped)


def _opt_str(item: Any, path: str | None) -> str | None:
    if not path:
        return None
    value = dig(item, path)
    return str(value) if value not in (None, "") else None


class StaticHTMLSource(BaseSource):
    """Server-rendered careers pages parsed with CSS selectors in ``field_map``.

    Required: ``item`` (container selector), ``title``. Optional: ``link`` (defaults
    to first ``a[href]``), ``location``, ``department``, ``description``.
    """

    def __init__(self, definition: SourceDefinition, http: SafeHttpClient) -> None:
        super().__init__(definition, http)
        if not {"item", "title"} <= definition.field_map.keys():
            raise ConfigurationError("html field_map requires 'item' and 'title' selectors")

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = _require_url(self.definition)
        result = await self._conditional_get(url, checkpoint)
        if result.not_modified:
            return self._not_modified(checkpoint, result)
        jobs, skipped = self._parse_listing(result.text, base_url=url)
        return self._finish(checkpoint, result, jobs, skipped)

    def _parse_listing(self, html: str, *, base_url: str) -> tuple[list[RawJob], list[dict[str, str]]]:
        fmap = self.definition.field_map
        tree = HTMLParser(html)
        skipped: list[dict[str, str]] = []
        jobs: list[RawJob] = []
        for node in tree.css(fmap["item"]):
            title = _select_text(node, fmap["title"])
            link_node = node.css_first(fmap.get("link", "a[href]"))
            href = link_node.attributes.get("href") if link_node is not None else None
            if not title or not href:
                skipped.append({"external_id": "?", "reason": "missing title or link"})
                continue
            absolute = urljoin(base_url, href)
            try:
                external_id = sha256_hex(canonicalize_url(absolute))[:32]
            except ValidationError:
                skipped.append({"external_id": "?", "reason": "invalid link"})
                continue
            description_node = node.css_first(fmap["description"]) if "description" in fmap else None
            job = self._build(
                skipped,
                external_id=external_id,
                title=title,
                url=absolute,
                location=_select_text(node, fmap.get("location")),
                department=_select_text(node, fmap.get("department")),
                description_html=description_node.html if description_node is not None else None,
                raw={"href": href, "title": title},
            )
            if job is not None:
                jobs.append(job)
        return jobs, skipped


def _select_text(node: Node, selector: str | None) -> str | None:
    if not selector:
        return None
    found = node.css_first(selector)
    if found is None:
        return None
    text = found.text(strip=True)
    return text or None


class DynamicHTMLSource(StaticHTMLSource):
    """JS-rendered careers pages. Playwright is used ONLY for this adapter.

    Every sub-request the browser makes is re-validated against the SSRF guard, so a
    malicious page cannot pivot the headless browser into the internal network.
    """

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = _require_url(self.definition)
        await self._http.validate_url(url)
        try:
            from playwright.async_api import Error as PlaywrightError  # noqa: PLC0415
            from playwright.async_api import Route, async_playwright  # noqa: PLC0415
        except ImportError as exc:
            raise ConfigurationError(
                "dynamic_html sources need the 'browser' extra (playwright) installed",
            ) from exc

        http = self._http

        async def guard(route: Route) -> None:
            try:
                await http.validate_url(route.request.url)
            except UnsafeUrlError, SourceFetchError:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        started = datetime.now(tz=UTC)
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=http.config.user_agent,
                        java_script_enabled=True,
                        service_workers="block",
                    )
                    await context.route("**/*", guard)
                    page = await context.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_NAVIGATION_TIMEOUT_MS)
                    html = (await page.content())[:PLAYWRIGHT_MAX_HTML_CHARS]
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            raise SourceFetchError("browser navigation failed", context={"url": url}) from exc

        elapsed_ms = (datetime.now(tz=UTC) - started).total_seconds() * 1000
        jobs, skipped = self._parse_listing(html, base_url=url)
        synthetic = FetchResult(
            url=url,
            status_code=200,
            headers={"content-type": "text/html"},
            content=html.encode("utf-8"),
            elapsed_ms=elapsed_ms,
        )
        return self._finish(checkpoint, synthetic, jobs, skipped)
