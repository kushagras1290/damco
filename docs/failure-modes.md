# Failure modes

| Failure | Detection | Behaviour |
|---|---|---|
| Source returns 429 | `SourceRateLimitedError` (honours `Retry-After`) | Fetch activity does not retry; polling workflow backs off exponentially (≥ Retry-After) |
| Source 5xx / timeout / DNS failure | `SourceFetchError` (retryable) | Activity retries (3×, exponential); poll recorded as failure |
| Repeated source failures | `consecutive_failures ≥ 5` | Circuit opens for 6 h (`circuit_open_until`); visible in UI; `Sync now` or re-enable resets |
| Board removed / bad token (404/4xx) | `SourceNotFoundError` (non-retryable) | Poll fails fast, error stored on the source |
| Payload schema drift | `SourceParseError`; per-item contract violations collected in `skipped` | Whole fetch fails loudly on structural drift; bad items are skipped and counted |
| robots.txt disallows | `RobotsDisallowedError` | Non-retryable; recorded on the source |
| SSRF attempt | `UnsafeUrlError` | Non-retryable; never fetched |
| OpenAI timeout / 429 / 5xx | `IntelligenceUnavailableError` | Retried 6× with backoff, then the stage **degrades**: evaluation continues with deterministic signals |
| OpenAI refusal / schema failure | `IntelligenceRefusalError` | Non-retryable; stage degrades |
| Job changed during evaluation | content-hash mismatch → `JobNotFoundError` | Evaluation stops; the newer version's workflow (different ID) proceeds |
| Duplicate evaluation start | Temporal `WorkflowAlreadyStarted` | Ignored (idempotent by workflow ID) |
| Notification transient failure | `NotificationDeliveryError` | Activity retries; dedupe row stays `pending` |
| Notification permanent failure | `NotificationRejectedError` | Marked `failed`, no retry |
| Duplicate notification | `UNIQUE(dedupe_key)` | Claim returns nothing for already-sent keys |
| Worker crash | Temporal | Workflows resume from history; activities retried |
| API ↔ Temporal unavailable | `WorkflowServiceError` | Write endpoints needing Temporal return 503; worker re-creates polling workflows on boot |
| DB unavailable | `/health/ready` | 503 readiness, pool pre-ping, statement timeout 15 s |
| Snapshot storage failure | `StorageError` (retryable) | Activity retries |
