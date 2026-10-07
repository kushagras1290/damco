"""Application settings, loaded from environment variables and validated at startup.

Missing required variables crash the process immediately (fail fast).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from jobpulse.core.jwks import validate_jwks
from jobpulse_core.ingestion.http import HttpClientConfig
from jobpulse_core.intelligence import IntelligenceConfig
from jobpulse_core.sources.ats import ATS_HOST_ALLOWLIST

# Must match the vector(N) columns created by the initial migration.
EMBEDDING_DIMENSIONS = 1536
RESEND_HOST = "api.resend.com"

Environment = Literal["local", "test", "staging", "production"]
_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # `FOO=` in .env means "unset", not "empty secret".
        env_ignore_empty=True,
    )

    environment: Environment = "local"
    # Enables the bundled sample-data "demo" source kind (never in production).
    demo_mode: bool = False
    service_name: str = "jobpulse-api"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # --- database ---------------------------------------------------------------
    database_url: PostgresDsn
    db_pool_size: Annotated[int, Field(ge=1, le=100)] = 10
    db_max_overflow: Annotated[int, Field(ge=0, le=100)] = 10
    db_pool_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0
    db_statement_timeout_ms: Annotated[int, Field(ge=100, le=600_000)] = 15_000
    # PgBouncer in transaction mode (e.g. Neon's "-pooler" endpoint) cannot use server-side
    # prepared statements. None = auto-detect from the host name.
    db_prepared_statements: bool | None = None
    # Non-owner role every transaction switches to so row-level security always applies
    # (owners and superusers bypass RLS). Created and granted by migration 0002.
    db_tenant_role: Annotated[str | None, Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")] = "jobpulse_app"
    # Realtime events need LISTEN, which PgBouncer transaction pooling cannot do. On a pooled
    # DATABASE_URL set this to the *direct* endpoint, or realtime is disabled (with a warning).
    database_listen_url: PostgresDsn | None = None

    # --- realtime (Server-Sent Events) -------------------------------------------
    sse_max_clients: Annotated[int, Field(ge=1, le=10_000)] = 200
    sse_queue_size: Annotated[int, Field(ge=10, le=10_000)] = 200
    sse_heartbeat_seconds: Annotated[float, Field(gt=0, le=60)] = 15.0

    # --- temporal ---------------------------------------------------------------
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_api_key: SecretStr | None = None
    temporal_tls: bool = False
    temporal_task_queue: str = "jobpulse"
    temporal_connect_timeout_seconds: Annotated[float, Field(gt=0, le=60)] = 10.0

    # --- auth -------------------------------------------------------------------
    # Public Ed25519 keys (JWK Set JSON) that verify web-minted tokens. Generate with
    # `python -m jobpulse.keys`. The private key lives only in the web app.
    api_jwt_jwks: str
    api_jwt_audience: str = "jobpulse-api"
    api_jwt_issuer: str = "jobpulse-web"
    public_demo_enabled: bool = True
    # open: anyone can sign up and gets a personal workspace; invite: accounts are created but
    # join workspaces only through invitations; closed: only platform admins can sign in.
    signup_policy: Literal["open", "invite", "closed"] = "open"
    # Immutable numeric GitHub user IDs granted OWNER. Logins are mutable and can be
    # re-registered after a rename, so they are never used for authorization.
    owner_github_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- http hardening ---------------------------------------------------------
    # Browsers never call the API directly (the web app proxies server-side), so by default
    # NO cross-origin access is granted. List exact origins only if a browser client needs it.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    max_request_bytes: Annotated[int, Field(ge=1024, le=10 * 1024 * 1024)] = 256 * 1024
    request_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0

    # --- rate limiting (sliding window, per verified identity / visitor / IP) ---------
    rate_limit_window_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    rate_limit_anonymous: Annotated[int, Field(ge=1, le=100_000)] = 120
    rate_limit_authenticated: Annotated[int, Field(ge=1, le=100_000)] = 600
    rate_limit_writes: Annotated[int, Field(ge=1, le=100_000)] = 60
    rate_limit_stream_connects: Annotated[int, Field(ge=1, le=10_000)] = 20

    # --- redis (shared ephemeral state: rate limits, cache, idempotency) -------------
    # Required in production (multi-instance correctness). Local dev may omit it.
    redis_url: SecretStr | None = None
    redis_socket_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 0.5
    redis_connect_timeout_seconds: Annotated[float, Field(gt=0, le=10)] = 1.0
    cache_ttl_seconds: Annotated[float, Field(ge=0, le=300)] = 5.0
    idempotency_ttl_seconds: Annotated[int, Field(ge=60, le=7 * 86_400)] = 86_400
    # Number of reverse proxies in front of the API that append to X-Forwarded-For.
    # 0 (default) = trust no forwarding headers (safe when exposed directly).
    trusted_proxy_count: Annotated[int, Field(ge=0, le=5)] = 0
    # Peers whose X-Forwarded-* headers uvicorn honours (platform proxy addresses).
    forwarded_allow_ips: str = "127.0.0.1"
    bind_host: str = "0.0.0.0"  # noqa: S104 - container port, fronted by the platform proxy
    port: Annotated[int, Field(ge=1, le=65535)] = 8000

    # --- outbound fetching ------------------------------------------------------
    outbound_enforce_allowlist: bool = True
    outbound_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
    outbound_connect_timeout_seconds: Annotated[float, Field(gt=0, le=30)] = 5.0
    outbound_read_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0
    outbound_total_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    outbound_max_response_bytes: Annotated[int, Field(ge=1024, le=128 * 1024 * 1024)] = 32 * 1024 * 1024
    outbound_respect_robots_txt: bool = True

    # --- AI -------------------------------------------------------------------
    openai_api_key: SecretStr | None = None
    openai_classification_model: str = "gpt-5-mini"
    openai_reasoning_model: str | None = "gpt-5"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 45.0
    openai_escalation_threshold: Annotated[float, Field(ge=0, le=1)] = 0.6
    openai_input_price_per_million: Annotated[float, Field(ge=0)] = 0.0
    openai_output_price_per_million: Annotated[float, Field(ge=0)] = 0.0
    openai_embedding_price_per_million: Annotated[float, Field(ge=0)] = 0.0
    embedding_dimensions: int = EMBEDDING_DIMENSIONS
    # Spend guards (UTC day). Soft caps: concurrent activities may overshoot slightly.
    openai_daily_request_limit: Annotated[int, Field(ge=0, le=1_000_000)] = 2000
    openai_daily_budget_usd: Annotated[float | None, Field(gt=0)] = None

    # --- storage ----------------------------------------------------------------
    storage_backend: Literal["local", "r2"] = "local"
    local_storage_path: Path = Path("./.data/snapshots")
    r2_account_id: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str | None = None
    r2_timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0

    # --- notifications ----------------------------------------------------------
    resend_api_key: SecretStr | None = None
    notification_from_email: str | None = None
    webhook_signing_secret: SecretStr | None = None
    dashboard_base_url: str = "http://localhost:3000"

    # --- observability ----------------------------------------------------------
    sentry_dsn: SecretStr | None = None
    sentry_traces_sample_rate: Annotated[float, Field(ge=0, le=1)] = 0.0
    otel_exporter_otlp_endpoint: str | None = None
    metrics_enabled: bool = True

    _split_origins = field_validator(
        "cors_allowed_origins", "outbound_allowed_hosts", "owner_github_ids", mode="before"
    )(_split_csv)

    @field_validator("api_jwt_jwks")
    @classmethod
    def _valid_jwks(cls, value: str) -> str:
        validate_jwks(value)  # fail fast on malformed keys or leaked private material
        return value

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+psycopg":
            msg = "DATABASE_URL must use the 'postgresql+psycopg://' scheme"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _cross_field(self) -> Self:
        if self.embedding_dimensions != EMBEDDING_DIMENSIONS:
            msg = f"EMBEDDING_DIMENSIONS must equal {EMBEDDING_DIMENSIONS} (schema vector size)"
            raise ValueError(msg)
        if self.storage_backend == "r2":
            missing = [
                name
                for name in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket")
                if getattr(self, name) in (None, "")
            ]
            if missing:
                msg = f"STORAGE_BACKEND=r2 requires: {', '.join(n.upper() for n in missing)}"
                raise ValueError(msg)
        bad_origins = [o for o in self.cors_allowed_origins if o != "*" and not _ORIGIN_RE.fullmatch(o)]
        if bad_origins:
            msg = f"CORS_ALLOWED_ORIGINS must be exact origins (scheme://host[:port]): {bad_origins}"
            raise ValueError(msg)
        if self.redis_url is not None and not self.redis_url.get_secret_value().startswith(("redis://", "rediss://")):
            msg = "REDIS_URL must use redis:// or rediss://"
            raise ValueError(msg)
        if self.environment == "production":
            if self.demo_mode:
                msg = "DEMO_MODE must be off in production"
                raise ValueError(msg)
            if "*" in self.cors_allowed_origins:
                msg = "wildcard CORS origin is not allowed in production"
                raise ValueError(msg)
            if self.storage_backend != "r2":
                msg = "production requires STORAGE_BACKEND=r2"
                raise ValueError(msg)
        if self.temporal_api_key is not None and not self.temporal_tls:
            msg = "TEMPORAL_API_KEY requires TEMPORAL_TLS=true"
            raise ValueError(msg)
        return self

    # --- derived configs -------------------------------------------------------

    @property
    def sqlalchemy_url(self) -> str:
        return str(self.database_url)

    @property
    def realtime_dsn(self) -> str | None:
        """libpq URL for the LISTEN connection, or None when realtime is unavailable."""
        if self.database_listen_url is not None:
            source = str(self.database_listen_url)
        elif self.use_prepared_statements:  # direct (non-pooled) connection
            source = self.sqlalchemy_url
        else:
            return None
        return source.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def use_prepared_statements(self) -> bool:
        if self.db_prepared_statements is not None:
            return self.db_prepared_statements
        hosts = self.database_url.hosts()
        return not any("-pooler" in (host.get("host") or "") for host in hosts)

    def http_client_config(
        self, *, extra_hosts: list[str] | None = None, robots: bool | None = None
    ) -> HttpClientConfig:
        hosts: tuple[str, ...] = ()
        if self.outbound_enforce_allowlist:
            hosts = tuple(
                dict.fromkeys([*ATS_HOST_ALLOWLIST, RESEND_HOST, *self.outbound_allowed_hosts, *(extra_hosts or [])]),
            )
        return HttpClientConfig(
            connect_timeout_seconds=self.outbound_connect_timeout_seconds,
            read_timeout_seconds=self.outbound_read_timeout_seconds,
            total_timeout_seconds=self.outbound_total_timeout_seconds,
            max_response_bytes=self.outbound_max_response_bytes,
            allowed_host_suffixes=hosts,
            respect_robots_txt=self.outbound_respect_robots_txt if robots is None else robots,
        )

    def intelligence_config(self) -> IntelligenceConfig:
        return IntelligenceConfig(
            api_key=self.openai_api_key.get_secret_value() if self.openai_api_key else None,
            classification_model=self.openai_classification_model,
            reasoning_model=self.openai_reasoning_model or None,
            embedding_model=self.openai_embedding_model,
            embedding_dimensions=self.embedding_dimensions,
            timeout_seconds=self.openai_timeout_seconds,
            escalation_threshold=self.openai_escalation_threshold,
            input_price_per_million=self.openai_input_price_per_million,
            output_price_per_million=self.openai_output_price_per_million,
            embedding_price_per_million=self.openai_embedding_price_per_million,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton. Raises pydantic ValidationError on bad config."""
    return Settings()  # type: ignore[call-arg]  # populated from environment
