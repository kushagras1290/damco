"""Immutable domain value objects.

These are transport-safe Pydantic models: they cross the Temporal payload boundary,
the OpenAI structured-output boundary and the REST boundary, so they contain only
JSON-serialisable primitives.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class SourceKind(StrEnum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    RSS = "rss"
    GENERIC_JSON = "generic_json"
    STATIC_HTML = "static_html"
    DYNAMIC_HTML = "dynamic_html"


class RemotePolicy(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class Seniority(StrEnum):
    INTERN = "intern"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    STAFF = "staff"
    PRINCIPAL = "principal"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    UNKNOWN = "unknown"


SENIORITY_RANK: dict[Seniority, int] = {
    Seniority.INTERN: 0,
    Seniority.JUNIOR: 1,
    Seniority.MID: 2,
    Seniority.SENIOR: 3,
    Seniority.LEAD: 4,
    Seniority.STAFF: 4,
    Seniority.MANAGER: 4,
    Seniority.PRINCIPAL: 5,
    Seniority.DIRECTOR: 6,
}


class EligibilityStatus(StrEnum):
    PENDING = "pending"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class SourceDefinition(BaseModel):
    """Everything an adapter needs to talk to one source. Persisted in ``sources.config``."""

    model_config = _FROZEN

    kind: SourceKind
    company_name: Annotated[str, Field(min_length=1, max_length=200)]
    company_domain: Annotated[str, Field(min_length=3, max_length=253)]
    board_token: Annotated[str | None, Field(max_length=200)] = None
    url: HttpUrl | None = None
    # Dotted paths used by GenericJSONSource / CSS selectors used by HTML sources.
    field_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_locator(self) -> Self:
        token_kinds = {SourceKind.GREENHOUSE, SourceKind.LEVER, SourceKind.ASHBY}
        if self.kind in token_kinds and not self.board_token:
            msg = f"{self.kind.value} sources require board_token"
            raise ValueError(msg)
        if self.kind not in token_kinds and self.url is None:
            msg = f"{self.kind.value} sources require url"
            raise ValueError(msg)
        return self


class SourceCheckpoint(BaseModel):
    """Opaque per-source cursor state used for conditional / incremental fetches."""

    model_config = ConfigDict(extra="forbid")

    etag: str | None = None
    last_modified: str | None = None
    cursor: dict[str, Any] = Field(default_factory=dict)
    last_seen_external_ids: list[str] = Field(default_factory=list)


class RawJob(BaseModel):
    """A job as emitted by an adapter, before normalization."""

    model_config = _FROZEN

    external_id: Annotated[str, Field(min_length=1, max_length=255)]
    title: Annotated[str, Field(min_length=1, max_length=500)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    company_name: str
    company_domain: str
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    description_html: str | None = None
    description_text: str | None = None
    published_at: datetime | None = None
    updated_at: datetime | None = None
    remote_hint: bool | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class NormalizedJob(BaseModel):
    """Canonical job representation produced by the normalizer."""

    model_config = _FROZEN

    external_id: str
    title: str
    normalized_title: str
    canonical_url: str
    company_name: str
    company_domain: str
    location: str | None
    normalized_location: str | None
    department: str | None
    employment_type: str | None
    description_html: str
    description_text: str
    published_at: datetime | None
    remote_policy: RemotePolicy
    seniority: Seniority
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    raw_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ExperienceRange(BaseModel):
    model_config = _FROZEN

    min: Annotated[float, Field(ge=0, le=50)] = 0
    max: Annotated[float, Field(ge=0, le=50)] = 50

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            msg = "experience.min must be <= experience.max"
            raise ValueError(msg)
        return self


class EligibilityPolicy(BaseModel):
    """Hard constraints. Mirrors the YAML policy in the architecture document."""

    model_config = _FROZEN

    allowed_locations: list[str] = Field(default_factory=lambda: ["India", "Worldwide"])
    allowed_work_models: list[RemotePolicy] = Field(default_factory=lambda: [RemotePolicy.REMOTE])
    allowed_timezones: list[str] = Field(
        default_factory=lambda: ["IST", "GMT", "BST", "CET", "EET"],
    )
    experience: ExperienceRange = Field(default_factory=lambda: ExperienceRange(min=3, max=6))
    excluded_regions: list[str] = Field(default_factory=lambda: ["US-only", "Canada-only"])
    # Tolerance (years) applied to experience bounds: a "7+ years" role is still
    # reachable for a 6-year candidate when tolerance >= 1.
    experience_tolerance_years: Annotated[float, Field(ge=0, le=5)] = 1.0


class CandidateProfile(BaseModel):
    """The searcher. Drives ranking; ``policy`` drives eligibility."""

    model_config = _FROZEN

    display_name: str = "Owner"
    target_roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    years_experience: Annotated[float, Field(ge=0, le=60)] = 0
    seniority: Seniority = Seniority.MID
    home_country: str = "India"
    timezone: str = "IST"
    policy: EligibilityPolicy = Field(default_factory=EligibilityPolicy)
    summary: Annotated[str, Field(max_length=8000)] = ""


class JobIntelligence(BaseModel):
    """Structured-output schema for LLM extraction (OpenAI strict JSON schema).

    Strict mode requires every field to be present, so optionality is modelled
    with ``| None`` rather than defaults.
    """

    model_config = ConfigDict(extra="forbid")

    remote_policy: RemotePolicy
    permitted_countries: list[str]
    excluded_countries: list[str]
    minimum_experience: float | None
    maximum_experience: float | None
    required_skills: list[str]
    preferred_skills: list[str]
    seniority: Seniority
    sponsorship_available: bool | None
    timezone_requirements: list[str]
    residency_requirement: str | None
    confidence: Annotated[float, Field(ge=0, le=1)]
