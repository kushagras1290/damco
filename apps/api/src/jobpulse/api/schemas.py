"""Request / response models for the REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator, model_validator

from jobpulse_core.domain.models import EligibilityPolicy, Seniority, SourceKind

ApplicationStatus = Literal["interested", "applied", "interviewing", "offer", "rejected", "withdrawn"]
_ORM = ConfigDict(from_attributes=True)
ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int


# ------------------------------------------------------------------ jobs


class JobSummary(BaseModel):
    id: uuid.UUID
    title: str
    company: str
    company_domain: str
    source_id: uuid.UUID
    source_name: str
    source_kind: str
    location: str | None
    remote_policy: str
    seniority: str
    url: str
    published_at: datetime | None
    first_seen_at: datetime
    eligibility_status: str
    workflow_state: str
    match_score: float | None
    closed: bool


class RuleOut(BaseModel):
    rule: str
    outcome: str
    evidence: str
    source: str


class EligibilityOut(BaseModel):
    model_config = _ORM
    id: uuid.UUID
    stage: str
    status: str
    rules: list[RuleOut]
    unresolved: list[str]
    policy_hash: str
    content_hash: str
    workflow_id: str | None
    created_at: datetime


class IntelligenceOut(BaseModel):
    model_config = _ORM
    model: str
    data: dict[str, Any]
    confidence: float
    escalated: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float
    created_at: datetime


class ScoreComponentOut(BaseModel):
    name: str
    value: float | None
    weight: float
    detail: str


class ScoreOut(BaseModel):
    model_config = _ORM
    final_score: float
    actionable: bool
    components: list[ScoreComponentOut]
    weights: dict[str, float]
    matched_skills: list[str]
    missing_skills: list[str]
    workflow_id: str | None
    created_at: datetime


class SnapshotOut(BaseModel):
    model_config = _ORM
    id: uuid.UUID
    snapshot_key: str
    snapshot_hash: str
    content_type: str
    size_bytes: int
    fetched_at: datetime


class VersionOut(BaseModel):
    model_config = _ORM
    version: int
    content_hash: str
    title: str
    location: str | None
    created_at: datetime


class NotificationOut(BaseModel):
    model_config = _ORM
    channel: str
    status: str
    attempts: int
    error: str | None
    sent_at: datetime | None
    created_at: datetime


class SimilarJobOut(BaseModel):
    id: uuid.UUID
    title: str
    company: str
    similarity: float


class ApplicationOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    company: str
    status: str
    notes: str
    applied_at: datetime | None
    created_at: datetime
    updated_at: datetime


class JobDetail(JobSummary):
    department: str | None
    employment_type: str | None
    description_html: str
    description_text: str
    version: int
    content_hash: str
    fingerprint: str
    duplicate_of_id: uuid.UUID | None
    eligibility: EligibilityOut | None
    eligibility_history: list[EligibilityOut]
    intelligence: IntelligenceOut | None
    score: ScoreOut | None
    snapshot: SnapshotOut | None
    versions: list[VersionOut]
    notifications: list[NotificationOut]
    application: ApplicationOut | None
    similar: list[SimilarJobOut]


class SnapshotContent(BaseModel):
    snapshot: SnapshotOut
    content: str
    truncated: bool


class ActionAccepted(BaseModel):
    workflow_id: str
    status: Literal["accepted"] = "accepted"


# ------------------------------------------------------------------ sources


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SourceKind
    name: ShortText
    company_name: ShortText
    company_domain: Annotated[str, Field(min_length=3, max_length=253, pattern=r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")]
    board_token: Annotated[str | None, Field(max_length=200, pattern=r"^[A-Za-z0-9_.-]+$")] = None
    url: HttpUrl | None = None
    field_map: dict[Annotated[str, Field(max_length=50)], Annotated[str, Field(max_length=300)]] = Field(
        default_factory=dict,
        max_length=20,
    )
    poll_interval_seconds: Annotated[int, Field(ge=300, le=86_400)] = 900
    min_poll_interval_seconds: Annotated[int, Field(ge=300, le=86_400)] = 300
    max_poll_interval_seconds: Annotated[int, Field(ge=300, le=86_400)] = 3600

    @model_validator(mode="after")
    def _interval_bounds(self) -> Self:
        if not self.min_poll_interval_seconds <= self.poll_interval_seconds <= self.max_poll_interval_seconds:
            msg = "require min_poll_interval <= poll_interval <= max_poll_interval"
            raise ValueError(msg)
        if self.url is not None and self.url.scheme != "https":
            msg = "source url must use https"
            raise ValueError(msg)
        return self


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: ShortText | None = None
    enabled: bool | None = None
    poll_interval_seconds: Annotated[int | None, Field(ge=300, le=86_400)] = None
    min_poll_interval_seconds: Annotated[int | None, Field(ge=300, le=86_400)] = None
    max_poll_interval_seconds: Annotated[int | None, Field(ge=300, le=86_400)] = None


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    company: str
    company_domain: str
    config: dict[str, Any]
    enabled: bool
    poll_interval_seconds: int
    min_poll_interval_seconds: int
    max_poll_interval_seconds: int
    consecutive_failures: int
    circuit_open_until: datetime | None
    last_polled_at: datetime | None
    last_success_at: datetime | None
    last_new_job_at: datetime | None
    last_error: str | None
    open_jobs: int
    created_at: datetime


# ------------------------------------------------------------------ runs


class RunOut(BaseModel):
    model_config = _ORM
    id: uuid.UUID
    workflow_id: str
    run_id: str | None
    workflow_type: str
    source_id: uuid.UUID | None
    job_id: uuid.UUID | None
    status: str
    stats: dict[str, Any]
    error: str | None
    started_at: datetime
    finished_at: datetime | None


# ------------------------------------------------------------------ profile


class ProfileOut(BaseModel):
    id: uuid.UUID
    display_name: str
    target_roles: list[str]
    skills: list[str]
    years_experience: float
    seniority: Seniority
    home_country: str
    timezone: str
    summary: str
    policy: EligibilityPolicy
    notification_email: str | None
    webhook_url: str | None
    notify_min_score: float
    notifications_enabled: bool
    has_embedding: bool
    updated_at: datetime


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: ShortText | None = None
    target_roles: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]] | None, Field(max_length=30)] = (
        None
    )
    skills: Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]] | None, Field(max_length=200)] = None
    years_experience: Annotated[float | None, Field(ge=0, le=60)] = None
    seniority: Seniority | None = None
    home_country: ShortText | None = None
    timezone: Annotated[str | None, Field(min_length=2, max_length=10)] = None
    summary: Annotated[str | None, Field(max_length=8000)] = None
    policy: EligibilityPolicy | None = None
    notification_email: EmailStr | None = None
    webhook_url: HttpUrl | None = None
    notify_min_score: Annotated[float | None, Field(ge=0, le=1)] = None
    notifications_enabled: bool | None = None

    @field_validator("webhook_url")
    @classmethod
    def _https_only(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            msg = "webhook_url must use https"
            raise ValueError(msg)
        return value


# ------------------------------------------------------------------ applications


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ApplicationStatus = "interested"
    notes: Annotated[str, Field(max_length=5000)] = ""
    applied_at: datetime | None = None


class ApplicationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ApplicationStatus | None = None
    notes: Annotated[str | None, Field(max_length=5000)] = None
    applied_at: datetime | None = None


# ------------------------------------------------------------------ decisions & system


class DecisionOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    job_title: str
    company: str
    stage: str
    status: str
    failed_rules: list[RuleOut]
    unresolved: list[str]
    created_at: datetime


class DashboardStats(BaseModel):
    jobs_by_status: dict[str, int]
    sources_total: int
    discovered_per_day: list[dict[str, Any]]
    score_histogram: list[dict[str, Any]]
    rejection_reasons: list[dict[str, Any]]
    runs_last_24h: dict[str, int]
    notifications: dict[str, int]
    llm_cost_usd: float
    top_matches: list[JobSummary]


class DependencyStatus(BaseModel):
    name: str
    ok: bool
    detail: str


class SystemStatus(BaseModel):
    environment: str
    version: str
    intelligence_enabled: bool
    storage_backend: str
    models: dict[str, str | None]
    dependencies: list[DependencyStatus]


class Me(BaseModel):
    subject: str
    login: str | None
    role: str
    authenticated: bool
