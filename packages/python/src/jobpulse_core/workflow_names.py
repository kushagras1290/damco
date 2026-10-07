"""Activity / workflow names and IDs shared by workflows, activities and API triggers.

Workflows reference activities by name so the workflow sandbox never has to import
the I/O-heavy activity implementations.
"""

from __future__ import annotations

# Activities
GET_SOURCE_SCHEDULE = "GetSourceScheduleActivity"
FETCH_JOBS = "FetchJobsActivity"
NORMALIZE_JOBS = "NormalizeJobsActivity"
STORE_JOBS = "StoreJobsActivity"
RECORD_POLL = "RecordPollActivity"
ELIGIBILITY = "EligibilityActivity"
ENRICHMENT = "EnrichmentActivity"
EMBEDDING = "EmbeddingActivity"
RANKING = "RankingActivity"
NOTIFICATION = "NotificationActivity"
RECORD_RUN_START = "RecordRunStartActivity"
RECORD_RUN_FINISH = "RecordRunFinishActivity"

# Workflows
SOURCE_POLLING_WORKFLOW = "SourcePollingWorkflow"
SOURCE_DISCOVERY_WORKFLOW = "SourceDiscoveryWorkflow"
JOB_EVALUATION_WORKFLOW = "JobEvaluationWorkflow"

# Signals / queries
POLL_NOW_SIGNAL = "poll_now"
STOP_SIGNAL = "stop"
STATUS_QUERY = "status"

# Errors that must never be retried by Temporal.
NON_RETRYABLE_ERRORS: list[str] = [
    "ConfigurationError",
    "ValidationError",
    "UnsafeUrlError",
    "RobotsDisallowedError",
    "SourceNotFoundError",
    "SourceParseError",
    "SourceResponseTooLargeError",
    "SourceUnavailableError",
    "JobNotFoundError",
    "IntelligenceRefusalError",
    "NotificationRejectedError",
    "SnapshotNotFoundError",
]


def polling_workflow_id(source_id: str) -> str:
    return f"source-polling-{source_id}"


def evaluation_workflow_id(job_id: str, content_hash: str) -> str:
    return f"job-eval-{job_id}-{content_hash[:16]}"
