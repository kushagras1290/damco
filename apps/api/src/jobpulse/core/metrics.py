"""Prometheus metrics shared by the API and the worker (names per architecture doc)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

JOBS_DISCOVERED = Counter("jobs_discovered_total", "New jobs discovered", ["source_kind"])
JOBS_REJECTED = Counter("jobs_rejected_total", "Jobs rejected by eligibility", ["stage", "rule"])
JOBS_MATCHED = Counter("jobs_matched_total", "Eligible jobs scored above the notify threshold")

SOURCE_FETCH_DURATION = Histogram(
    "source_fetch_duration_seconds",
    "Time spent fetching a source",
    ["source_kind"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
SOURCE_FAILURES = Counter("source_failures_total", "Source fetch failures", ["source_kind", "error"])
SOURCE_PAYLOAD_NEAR_LIMIT = Counter(
    "source_payload_near_limit_total", "Fetched payloads above 75% of the response size limit", ["source_kind"]
)
SUSPICIOUS_LISTINGS = Counter(
    "source_suspicious_listings_total", "Listings ignored for closures (e.g. sudden shrink)", ["reason"]
)

LLM_REQUESTS = Counter("llm_requests_total", "LLM / embedding requests", ["model", "kind", "outcome"])
LLM_COST_USD = Counter("llm_cost_usd_total", "Estimated LLM spend in USD", ["model"])
LLM_BUDGET_EXHAUSTED = Counter("llm_budget_exhausted_total", "Enrichments skipped by the daily spend guard", ["limit"])

NOTIFICATIONS_SENT = Counter("notifications_sent_total", "Notifications delivered", ["channel", "outcome"])
JOB_PROCESSING_DURATION = Histogram(
    "job_processing_duration_seconds",
    "Duration of evaluation stages",
    ["stage"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)

HTTP_REQUESTS = Counter("http_requests_total", "API requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "API request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
