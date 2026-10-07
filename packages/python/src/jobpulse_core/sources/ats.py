"""Adapters for applicant-tracking systems with public JSON job-board APIs."""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from jobpulse_core.domain.models import RawJob, SourceCheckpoint
from jobpulse_core.errors import SourceParseError
from jobpulse_core.sources.base import BaseSource, DiscoveryResult, parse_datetime

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_API = "https://api.lever.co/v0/postings/{token}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"

ATS_HOST_ALLOWLIST: tuple[str, ...] = (
    "boards-api.greenhouse.io",
    "api.lever.co",
    "api.ashbyhq.com",
)


def _token(value: str | None) -> str:
    if not value:
        raise SourceParseError("board_token missing")
    return quote(value, safe="")


class GreenhouseSource(BaseSource):
    """https://developers.greenhouse.io/job-board.html"""

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = GREENHOUSE_API.format(token=_token(self.definition.board_token))
        result = await self._conditional_get(url, checkpoint)
        if result.not_modified:
            return self._not_modified(checkpoint, result)
        payload = self._decode_json(result)
        items = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise SourceParseError("greenhouse payload missing 'jobs' list", context={"url": url})

        skipped: list[dict[str, str]] = []
        jobs: list[RawJob] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            location = (item.get("location") or {}).get("name")
            departments = item.get("departments") or []
            job = self._build(
                skipped,
                external_id=str(item.get("id", "")),
                title=item.get("title") or "",
                url=item.get("absolute_url") or "",
                location=location,
                department=departments[0].get("name") if departments and isinstance(departments[0], dict) else None,
                description_html=item.get("content"),
                published_at=parse_datetime(item.get("first_published") or item.get("updated_at")),
                updated_at=parse_datetime(item.get("updated_at")),
                raw=item,
            )
            if job is not None:
                jobs.append(job)
        return self._finish(checkpoint, result, jobs, skipped)


class LeverSource(BaseSource):
    """https://github.com/lever/postings-api"""

    WORKPLACE_REMOTE: ClassVar[dict[str, bool | None]] = {"remote": True, "onsite": False, "hybrid": None}

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = LEVER_API.format(token=_token(self.definition.board_token))
        result = await self._conditional_get(url, checkpoint)
        if result.not_modified:
            return self._not_modified(checkpoint, result)
        payload = self._decode_json(result)
        if not isinstance(payload, list):
            raise SourceParseError("lever payload must be a list", context={"url": url})

        skipped: list[dict[str, str]] = []
        jobs: list[RawJob] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            categories: dict[str, Any] = item.get("categories") or {}
            description_parts = [item.get("description") or ""]
            for block in item.get("lists") or []:
                if isinstance(block, dict):
                    description_parts.append(f"<h3>{block.get('text', '')}</h3><ul>{block.get('content', '')}</ul>")
            description_parts.append(item.get("additional") or "")
            all_locations = categories.get("allLocations") or []
            location = categories.get("location") or (", ".join(all_locations) if all_locations else None)
            job = self._build(
                skipped,
                external_id=str(item.get("id", "")),
                title=item.get("text") or "",
                url=item.get("hostedUrl") or "",
                location=location,
                department=categories.get("team") or categories.get("department"),
                employment_type=categories.get("commitment"),
                description_html="".join(description_parts),
                published_at=parse_datetime(item.get("createdAt")),
                remote_hint=self.WORKPLACE_REMOTE.get(str(item.get("workplaceType", "")).lower()),
                raw=item,
            )
            if job is not None:
                jobs.append(job)
        return self._finish(checkpoint, result, jobs, skipped)


class AshbySource(BaseSource):
    """https://developers.ashbyhq.com/docs/public-job-posting-api"""

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        url = ASHBY_API.format(token=_token(self.definition.board_token))
        result = await self._conditional_get(url, checkpoint)
        if result.not_modified:
            return self._not_modified(checkpoint, result)
        payload = self._decode_json(result)
        items = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise SourceParseError("ashby payload missing 'jobs' list", context={"url": url})

        skipped: list[dict[str, str]] = []
        jobs: list[RawJob] = []
        for item in items:
            if not isinstance(item, dict) or item.get("isListed") is False:
                continue
            secondary = [
                loc.get("location")
                for loc in item.get("secondaryLocations") or []
                if isinstance(loc, dict) and loc.get("location")
            ]
            location_parts = [item.get("location"), *secondary]
            location = ", ".join(part for part in location_parts if part) or None
            workplace = str(item.get("workplaceType") or "").lower()
            remote_hint: bool | None = bool(item["isRemote"]) if "isRemote" in item else None
            if workplace == "hybrid":
                remote_hint = None
            job = self._build(
                skipped,
                external_id=str(item.get("id", "")),
                title=item.get("title") or "",
                url=item.get("jobUrl") or item.get("applyUrl") or "",
                location=location,
                department=item.get("department") or item.get("team"),
                employment_type=item.get("employmentType"),
                description_html=item.get("descriptionHtml"),
                description_text=item.get("descriptionPlain"),
                published_at=parse_datetime(item.get("publishedAt")),
                remote_hint=remote_hint,
                raw=item,
            )
            if job is not None:
                jobs.append(job)
        return self._finish(checkpoint, result, jobs, skipped)
