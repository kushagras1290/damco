"""Temporal workflow / activity payload contracts.

Pure Pydantic models with no I/O imports so they are safe to load inside the Temporal
workflow sandbox. Payloads carry IDs and storage keys, never full job bodies, which
keeps workflow history small (well under Temporal's payload limits).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_CFG = ConfigDict(frozen=True, extra="forbid")


class SourceRef(BaseModel):
    model_config = _CFG
    source_id: str


class PollingState(BaseModel):
    """Carried across continue-as-new for SourcePollingWorkflow."""

    model_config = _CFG
    source_id: str
    iterations: int = 0
    consecutive_failures: int = 0


class FetchOutcome(BaseModel):
    model_config = _CFG
    source_id: str
    source_kind: str
    staging_key: str | None
    discovered: int
    skipped: int
    not_modified: bool
    fetch_ms: float
    skipped_reason: str | None = None


class NormalizeInput(BaseModel):
    model_config = _CFG
    source_id: str
    staging_key: str


class NormalizeOutcome(BaseModel):
    model_config = _CFG
    source_id: str
    normalized_key: str
    normalized: int
    failed: int


class StoreInput(BaseModel):
    model_config = _CFG
    source_id: str
    normalized_key: str


class StoreOutcome(BaseModel):
    model_config = _CFG
    source_id: str
    new_jobs: int
    updated_jobs: int
    unchanged_jobs: int
    duplicate_jobs: int
    closed_jobs: int
    jobs_to_evaluate: list[str] = Field(default_factory=list)
    content_hashes: dict[str, str] = Field(default_factory=dict)


class DiscoveryResultSummary(BaseModel):
    model_config = _CFG
    source_id: str
    status: str
    new_jobs: int = 0
    updated_jobs: int = 0
    evaluations_started: int = 0
    not_modified: bool = False
    error: str | None = None
    error_type: str | None = None
    retry_after_seconds: float | None = None


class PollRecord(BaseModel):
    model_config = _CFG
    source_id: str
    success: bool
    new_jobs: int
    next_interval_seconds: int
    circuit_open_seconds: int | None
    error: str | None


class SourceSchedule(BaseModel):
    model_config = _CFG
    source_id: str
    enabled: bool
    poll_interval_seconds: int
    min_poll_interval_seconds: int
    max_poll_interval_seconds: int
    consecutive_failures: int
    circuit_open_remaining_seconds: int


class JobRef(BaseModel):
    model_config = _CFG
    job_id: str
    content_hash: str | None = None
    force: bool = False


class StageResult(BaseModel):
    model_config = _CFG
    job_id: str
    proceed: bool
    eligible: bool | None = None
    detail: str = ""
    score: float | None = None


class EvaluationSummary(BaseModel):
    model_config = _CFG
    job_id: str
    eligible: bool
    score: float | None
    notified: int
    stages: list[str]


class RunRecord(BaseModel):
    model_config = _CFG
    workflow_id: str
    run_id: str
    workflow_type: str
    source_id: str | None = None
    job_id: str | None = None


class RunFinish(BaseModel):
    model_config = _CFG
    workflow_id: str
    run_id: str
    status: str
    stats: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    error: str | None = None
